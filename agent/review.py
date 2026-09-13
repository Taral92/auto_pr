from collections.abc import Callable

import hashlib
import json
import os
import tempfile
import time
import uuid
from datetime import datetime

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
) -> ReviewResult:
    """`token` may be a string (CLI, PAT) or a zero-arg provider (App).

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
            _write_trace(result, [])
            return result

        tmp = tempfile.mkdtemp(prefix="auto-pr-")
        clone_head(tmp, owner, repo, number, get_token(), head_sha)
        db = PROJECT_ROOT / "runs" / "checkpoints.db"
        db.parent.mkdir(exist_ok=True)
        with SqliteSaver.from_conn_string(str(db)) as saver:
            saver.setup()
            app = build_graph().compile(checkpointer=saver)
            # `thread_id` stays `run_id`, so one run keeps one checkpoint
            # lineage - but the worker reuses that id for every attempt, and
            # anything this input did not name survived from the attempt that
            # failed. `fresh_state` names every field; see its docstring.
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
        result = _finish(
            final, pr_url, owner, repo, number, get_token, dry_run, t0, True,
            trace, post,
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
    _write_trace(result, trace)
    return result


def key_sha(state) -> str:
    return state.get("head_sha") or ""


def _write_trace(result: ReviewResult, trace: list) -> None:
    runs_dir = PROJECT_ROOT / "runs"
    runs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out = runs_dir / f"{ts}.json"
    payload = {
        "timestamp": ts,
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
