from collections.abc import Callable

import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from pydantic import ValidationError

from config import ROOT, get_settings
from core.models import PublishedFinding, ReviewResult
from gh.client import MARKER
from gh import (
    already_reviewed,
    posted_finding_keys,
    build_review,
    clone_head,
    get_diff,
    get_pr,
    post_review,
    rmtree,
    too_large_payload,
)
from .graph import RECURSION_LIMIT, build_graph
from .graph_state import ReviewState, fresh_state
from .nodes import system_prompt
from .runtime import run_id_var, trace_holder

PROJECT_ROOT = ROOT

#: One definition, three callers: `review_pr`, `sweep_checkpoints`, and the
#: tests. It is deliberately container-local - see docker-compose.yml.
CHECKPOINT_DB = PROJECT_ROOT / "runs" / "checkpoints.db"


def sweep_checkpoints(db: "Path | None" = None) -> int:
    """Delete every checkpoint thread in the local DB. Returns threads removed.

    Safe to call at worker startup and nowhere else. Everything in this file at
    boot is orphaned BY CONSTRUCTION: no production path resumes a thread -
    both `app.invoke` calls pass a complete input, and none passes `None` - so
    a checkpoint is dead the moment the invoke that wrote it returns. The only
    rows that survive a process are the ones a crash stopped `review_pr` from
    deleting, and no future run will read those either.

    This is NOT a reaper. It never runs against a live thread, because the
    process that calls it is the only one that writes to this file and it has
    not started reviewing yet. A time-based reaper would have to guess whether
    a thread is live, and firing mid-run neither reclaims the space nor leaves
    clean history - the run simply rewrites the rows behind it.

    VACUUM because `delete_thread` frees pages for reuse without returning them
    to the OS. Deleting 145MB of rows otherwise leaves a 145MB file.
    """
    path = CHECKPOINT_DB if db is None else db
    if not path.exists():
        return 0
    with SqliteSaver.from_conn_string(str(path)) as saver:
        saver.setup()
        threads = [
            row[0]
            for row in saver.conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall()
        ]
        for thread_id in threads:
            saver.delete_thread(thread_id)
        if threads:
            saver.conn.execute("VACUUM")
    return len(threads)


def idempotency_key(owner: str, repo: str, number: int, head_sha: str) -> str:
    """The existing review marker key. One definition, two callers.

    Deliberately NOT keyed on prompt_sha: editing the prompt would otherwise
    re-review every open PR. A re-review comes from a new commit, which
    head_sha already covers. `post_payload` reuses this so a retried post is
    recognised as the same review rather than posted twice.
    """
    return hashlib.sha256(
        f"{owner}/{repo}#{number}@{head_sha}".encode()
    ).hexdigest()[:16]


def post_payload(
    owner: str,
    repo: str,
    number: int,
    token: "str | Callable[[], str]",
    payload: dict,
    head_sha: str,
) -> bool:
    """Post an ALREADY COMPUTED review. Returns True if it posted.

    This is the half of `_finish` that talks to GitHub, split out so the worker
    can retry it against a payload it has already persisted - without running
    the model pipeline again. Idempotency is the existing marker scheme, not a
    new one: the body carries `MARKER.format(key=...)` and `already_reviewed`
    looks for exactly that, so a retry after an ambiguous failure is a no-op
    rather than a duplicate review.
    """
    get_token = token if callable(token) else (lambda: token)
    key = idempotency_key(owner, repo, number, head_sha)
    if already_reviewed(owner, repo, number, get_token(), key):
        print(f"already reviewed {key}; skipping post")
        return False
    post_review(owner, repo, number, get_token(), payload)
    return True


def review_pr(
    owner: str,
    repo: str,
    number: int,
    token: "str | Callable[[], str]",
    dry_run: bool = False,
    post: bool = True,
    attempt: int = 0,
    write_trace: bool | None = None,
) -> ReviewResult:
    """`token` may be a string (CLI, PAT) or a zero-arg provider (App).

    `attempt` only names the trace file. The worker retries a run under one
    `run_id`, so without it two attempts of the same run collide.

    `write_trace` overrides the `WRITE_TRACE` setting: None follows it (off),
    which is what the worker wants because Postgres already has the trace. The
    CLI passes True, where the file is the only output there is.

    A GitHub App installation token lives one hour and a review can start at
    minute 58. Holding a provider rather than a string means every call mints
    or reuses a live token instead of dying on a stale one.
    """
    get_token = token if callable(token) else (lambda: token)
    settings = get_settings()
    pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    t0 = time.monotonic()
    try:
        run_id = run_id_var.get()
    except LookupError:
        run_id = str(uuid.uuid4())
    trace: list = []
    trace_tok = trace_holder.set(trace)
    tmp: str | None = None
    result: ReviewResult | None = None
    try:
        meta = get_pr(owner, repo, number, get_token())
        head_sha = meta["head"]["sha"]
        diff = get_diff(owner, repo, number, get_token())
        if len(diff.encode("utf-8")) > settings.max_diff_bytes:
            # Same contract as the normal path below: `post=False` computes the
            # payload and stops, so the caller persists it before anything
            # reaches GitHub. This branch used to POST unconditionally and then
            # report `published`, so the worker committed `post_pending` and
            # posted the same notice a second time - and the payload carried no
            # marker, so `already_reviewed` could not catch it either.
            payload = too_large_payload(
                head_sha, idempotency_key(owner, repo, number, head_sha)
            )
            posted = False
            if post and not dry_run:
                posted = post_payload(
                    owner, repo, number, get_token, payload, head_sha
                )
            result = ReviewResult(
                pr_url=pr_url,
                owner=owner,
                repo=repo,
                number=number,
                head_sha=head_sha,
                dry_run=dry_run,
                posted=posted,
                payload=payload,
                grounding={"grounded": 0, "near": 0, "ungrounded": 0},
                anchoring={"inline": 0, "summary": 0, "dropped": 0},
                findings=[],
                prompt_sha=hashlib.sha256(system_prompt().encode()).hexdigest(),
                model=settings.model,
                tokens_in=0,
                tokens_out=0,
                wall_clock_s=round(time.monotonic() - t0, 3),
                temp_dir_removed=True,
                status="published",
                corpus=[{"source": "diff", "text": diff}],
                trace=[],
            )
            _write_trace(result, [], run_id, attempt, write_trace)
            return result

        tmp = tempfile.mkdtemp(prefix="auto-pr-")
        clone_head(tmp, owner, repo, number, get_token(), head_sha)
        CHECKPOINT_DB.parent.mkdir(exist_ok=True)
        with SqliteSaver.from_conn_string(str(CHECKPOINT_DB)) as saver:
            saver.setup()
            app = build_graph().compile(checkpointer=saver)
            # `thread_id` stays `run_id`, so one run keeps one checkpoint
            # lineage - but the worker reuses that id for every attempt, and
            # anything this input did not name survived from the attempt that
            # failed. `fresh_state` names every field; see its docstring.
            #
            # The `finally` below then drops the thread. LangGraph checkpoints
            # the WHOLE state at every superstep, and `messages` and `corpus`
            # grow all run, so the cost is quadratic in turns: a 9-turn review
            # of a 277KB diff measured 26-31MB, and five of them shared a 145MB
            # file that nothing ever read again. Deleting on exit is safe
            # precisely because nothing resumes - see `sweep_checkpoints`.
            try:
                final: ReviewState = app.invoke(
                    fresh_state(
                        run_id=run_id,
                        owner=owner,
                        repo=repo,
                        number=number,
                        dry_run=dry_run,
                        head_sha=head_sha,
                        diff=diff,
                        workspace=tmp,
                        corpus=[{"source": "diff", "text": diff}],
                        started_at=t0,
                    ),
                    config={
                        "configurable": {"thread_id": run_id},
                        "recursion_limit": RECURSION_LIMIT,
                    },
                )
            finally:
                # After invoke returns, success or failure. Never before: the
                # rows are load-bearing for Pregel's own mechanics WHILE the
                # graph runs, and only dead once it has stopped.
                try:
                    saver.delete_thread(run_id)
                except Exception as e:            # noqa: BLE001
                    # Never mask the exception we are unwinding: a locked or
                    # full database here would turn a retryable TransientError
                    # into an unexplained `failed`. The leak is one thread, and
                    # the boot sweep is exactly the backstop for it.
                    print(f"checkpoint cleanup failed for {run_id}: {e}")
        result = _finish(
            final, pr_url, owner, repo, number, get_token, dry_run, t0, True,
            trace, post, run_id, attempt, write_trace,
        )
        return result
    finally:
        trace_holder.reset(trace_tok)
        if tmp is not None:
            rmtree(tmp)
            gone = not os.path.exists(tmp)
            if result is not None:
                result.temp_dir_removed = gone


def _finish(
    state: ReviewState,
    pr_url: str,
    owner: str,
    repo: str,
    number: int,
    get_token: "Callable[[], str]",
    dry_run: bool,
    t0: float,
    temp_dir_removed: bool,
    trace: list,
    post: bool = True,
    run_id: str = "",
    attempt: int = 0,
    write_trace: bool | None = None,
) -> ReviewResult:
    settings = get_settings()
    status = state.get("status") or "running"
    summary = state.get("summary") or ""
    items = list(state.get("findings") or [])
    payload: dict
    published: list[dict]
    tally: dict
    posted = False
    if status == "failed":
        published = items
        tally = {"inline": 0, "summary": 0, "dropped": 0}
        payload = {
            "commit_id": state.get("head_sha") or "",
            "event": "COMMENT",
            "body": state.get("error") or "failed",
            "comments": [],
        }
    else:
        already = (
            set() if dry_run
            else posted_finding_keys(owner, repo, number, get_token())
        )
        published, payload, tally = build_review(
            items, state.get("diff") or "", state["head_sha"], summary,
            already=already,
        )
        # Idempotency. GitHub redelivers webhooks and Actions re-run; without
        # this one PR collects the same comments several times. See
        # `idempotency_key` for why prompt_sha is deliberately excluded.
        key = idempotency_key(owner, repo, number, state.get("head_sha") or "")
        payload["body"] = f"{payload.get('body','')}\n\n{MARKER.format(key=key)}".strip()
        # `post=False` stops here: the payload is complete and the caller
        # persists it before anything is sent to GitHub, so a failed post costs
        # a retry of the post and not of the whole review.
        if post and not dry_run:
            if post_payload(owner, repo, number, get_token, payload, key_sha(state)):
                posted = True
                for f in published:
                    if f.get("anchored") != "dropped":
                        f["posted"] = True
        if status == "running":
            status = "published"

    findings: list[PublishedFinding] = []
    for item in published:
        try:
            findings.append(
                PublishedFinding(
                    severity=item.get("severity") or "nit",
                    category=item.get("category") or "maintainability",
                    path=item.get("path") or item.get("file") or "",
                    line=item.get("line"),
                    title=item.get("title") or "",
                    body=item.get("body") or "",
                    evidence=item.get("evidence") or "",
                    verdict=item.get("verdict") or "ungrounded",
                    anchored=item.get("anchored") or "dropped",
                    posted=bool(item.get("posted")),
                )
            )
        except ValidationError:
            continue

    result = ReviewResult(
        pr_url=pr_url,
        owner=owner,
        repo=repo,
        number=number,
        head_sha=state.get("head_sha") or "",
        dry_run=dry_run,
        posted=posted,
        payload=payload,
        grounding=state.get("grounding")
        or {"grounded": 0, "near": 0, "ungrounded": 0},
        anchoring=tally,
        findings=findings,
        prompt_sha=state.get("prompt_sha")
        or hashlib.sha256(system_prompt().encode()).hexdigest(),
        model=settings.model,
        tokens_in=int(state.get("tokens_in") or 0),
        tokens_out=int(state.get("tokens_out") or 0),
        iterations=int(state.get("iterations") or 0),
        wall_clock_s=round(time.monotonic() - t0, 3),
        temp_dir_removed=temp_dir_removed,
        error=state.get("error"),
        status=status if status != "too_large" else "published",
        corpus=list(state.get("corpus") or []),
        trace=trace,
    )
    _write_trace(result, trace, run_id, attempt, write_trace)
    return result


def key_sha(state) -> str:
    return state.get("head_sha") or ""


def _write_trace(
    result: ReviewResult,
    trace: list,
    run_id: str = "",
    attempt: int = 0,
    enabled: bool | None = None,
) -> None:
    """Write one run's trace to runs/, if trace files are switched on.

    Off by default on the worker. `record_result` already writes `trace`,
    `corpus` and `payload` onto the runs row and `/api/runs/{id}/trace` serves
    them from there, so on the worker these files were a second copy of private
    source that nothing read, on a path with no volume and no rotation.

    The name carries run_id and attempt. It used to be the timestamp alone at
    one-second resolution, so two reviews finishing in the same second - two
    workers, or two attempts of one run - silently overwrote each other.
    """
    if enabled is None:
        enabled = get_settings().write_trace
    if not enabled:
        return
    runs_dir = PROJECT_ROOT / "runs"
    runs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = runs_dir / f"{ts}.{run_id or 'norun'}.{attempt}.json"
    payload = {
        "timestamp": ts,
        "run_id": run_id,
        "attempt": attempt,
        "pr_url": result.pr_url,
        "head_sha": result.head_sha,
        "prompt_sha": result.prompt_sha,
        "model": result.model,
        "dry_run": result.dry_run,
        "posted": result.posted,
        "grounding": result.grounding,
        "anchoring": result.anchoring,
        "wall_clock_s": result.wall_clock_s,
        "tokens_in_total": result.tokens_in,
        "tokens_out_total": result.tokens_out,
        "temp_dir_removed": result.temp_dir_removed,
        "steps": trace,
        "findings": [f.model_dump() for f in result.findings],
        "payload": result.payload,
        "error": result.error,
        "status": result.status,
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out}")
    _prune_traces(runs_dir)


def _prune_traces(runs_dir: Path) -> None:
    """Keep the newest MAX_TRACE_FILES traces; drop the rest.

    Only `*.json`, so `checkpoints.db` and its WAL sidecars are never a
    candidate. Ties on mtime break on name, which carries the timestamp, so
    the order is total and the prune is deterministic.
    """
    keep = get_settings().max_trace_files
    if keep <= 0:
        return
    files = sorted(
        runs_dir.glob("*.json"),
        key=lambda f: (f.stat().st_mtime, f.name),
        reverse=True,
    )
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass          # a concurrent prune got there first; nothing to do
