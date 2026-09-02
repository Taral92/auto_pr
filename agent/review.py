import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import github
from .anchor import locate_in_diff, parse_anchorable
from .grounding import counts as grounding_counts
from .grounding import ground
from .loop import run, system_prompt
from .models import Finding, PublishedFinding, ReviewFindings, ReviewResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIFF_CAP = 400 * 1024
TOO_LARGE = "Diff too large to review."


def review_pr(
    owner: str,
    repo: str,
    number: int,
    token: str,
    dry_run: bool = False,
) -> ReviewResult:
    pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"
    t0 = time.monotonic()
    tmp: str | None = None
    model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MODEL", "claude-sonnet-4-20250514")
    prompt_sha = hashlib.sha256(system_prompt().encode()).hexdigest()
    done: dict | None = None
    try:
        meta = github.get_pr(owner, repo, number, token)
        head_sha = meta["head"]["sha"]
        diff = github.get_diff(owner, repo, number, token)
        if len(diff.encode("utf-8")) > DIFF_CAP:
            done = dict(
                head_sha=head_sha,
                payload=_payload(head_sha, TOO_LARGE, []),
                grounding={"grounded": 0, "near": 0, "ungrounded": 0},
                anchoring={"inline": 0, "summary": 0},
                findings=[],
                tokens_in=0,
                tokens_out=0,
                trace=[],
            )
        else:
            tmp = tempfile.mkdtemp(prefix="auto-pr-")
            _clone_head(tmp, owner, repo, number, token, head_sha)
            trace: list = []
            review = run(diff=diff, repo_root=tmp, trace=trace)
            rows = ground(review.findings, diff, _tool_results(trace))
            published, payload = _build_payload(review, rows, diff, head_sha)
            done = dict(
                head_sha=head_sha,
                payload=payload,
                grounding=grounding_counts(rows),
                anchoring=_anchor_counts(published),
                findings=published,
                tokens_in=sum(s.get("api_in") or 0 for s in trace),
                tokens_out=sum(s.get("api_out") or 0 for s in trace),
                trace=trace,
            )
    finally:
        removed = True
        if tmp is not None:
            _rmtree(tmp)
            removed = not os.path.exists(tmp)

    assert done is not None
    return _finish(
        pr_url,
        owner,
        repo,
        number,
        done["head_sha"],
        dry_run,
        done["payload"],
        done["grounding"],
        done["anchoring"],
        done["findings"],
        prompt_sha,
        model,
        done["tokens_in"],
        done["tokens_out"],
        t0,
        removed,
        token,
        None,
        done["trace"],
    )


def _clone_head(
    dest: str, owner: str, repo: str, number: int, token: str, head_sha: str
) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    origin = f"https://github.com/{owner}/{repo}.git"
    auth = ["-c", f"http.extraHeader=Authorization: Bearer {token}"]

    def git(args: list[str], extra: list[str] | None = None) -> str:
        cmd = ["git", "-C", dest, *(extra or []), *args]
        try:
            proc = subprocess.run(
                cmd, check=True, capture_output=True, text=True, env=env
            )
            return proc.stdout
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or e.stdout or "").strip()
            raise RuntimeError(
                f"git {args[0]} failed: {_redact(msg, token)}"
            ) from None

    git(["init"])
    git(["remote", "add", "origin", origin])
    git(["fetch", "--depth=1", "origin", f"pull/{number}/head"], extra=auth)
    git(["checkout", "FETCH_HEAD"])
    got = git(["rev-parse", "HEAD"]).strip()
    if got != head_sha:
        raise RuntimeError(
            f"HEAD {got} != head.sha {head_sha}; PR moved mid-run"
        )


def _tool_results(trace: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for step in trace:
        for call in step.get("tools") or []:
            path = (call.get("input") or {}).get("path", "")
            name = call.get("name") or "tool"
            label = f"{name}:{path}" if path else name
            out.append((label, call.get("result") or ""))
    return out


def _finding_body(finding: Finding) -> str:
    parts = [finding.title, "", finding.description]
    if finding.recommendation:
        parts.extend(["", f"Recommendation: {finding.recommendation}"])
    return "\n".join(parts).strip()


def _build_payload(
    review: ReviewFindings,
    rows: list,
    diff: str,
    head_sha: str,
) -> tuple[list[PublishedFinding], dict]:
    anchorable = parse_anchorable(diff)
    published: list[PublishedFinding] = []
    comments: list[dict] = []
    summary_items: list[str] = []
    for finding, verdict, _source in rows:
        body = _finding_body(finding)
        if verdict != "grounded":
            published.append(
                PublishedFinding(
                    severity=finding.severity,
                    category=finding.category,
                    path=finding.file,
                    line=None,
                    title=finding.title,
                    body=body,
                    evidence=finding.evidence,
                    verdict=verdict,
                    anchored="dropped",
                )
            )
            continue
        loc = locate_in_diff(diff, finding.evidence)
        if loc is not None and loc[1] in anchorable.get(loc[0], set()):
            path, line = loc
            comments.append(
                {
                    "path": path,
                    "line": line,
                    "side": "RIGHT",
                    "body": body,
                }
            )
            published.append(
                PublishedFinding(
                    severity=finding.severity,
                    category=finding.category,
                    path=path,
                    line=line,
                    title=finding.title,
                    body=body,
                    evidence=finding.evidence,
                    verdict=verdict,
                    anchored="inline",
                )
            )
        else:
            path = loc[0] if loc is not None else finding.file
            line = loc[1] if loc is not None else None
            loc_label = f"{path}:{line}" if line is not None else path
            summary_items.append(f"**{loc_label}** — {body}")
            published.append(
                PublishedFinding(
                    severity=finding.severity,
                    category=finding.category,
                    path=path,
                    line=line,
                    title=finding.title,
                    body=body,
                    evidence=finding.evidence,
                    verdict=verdict,
                    anchored="summary",
                )
            )
    body_parts = [review.summary.strip()] if review.summary.strip() else []
    body_parts.extend(summary_items)
    if not comments and not summary_items:
        if not body_parts:
            body_parts.append("Reviewed, nothing found.")
    payload = _payload(head_sha, "\n\n".join(body_parts).strip(), comments)
    return published, payload


def _payload(commit_id: str, body: str, comments: list[dict]) -> dict:
    return {
        "commit_id": commit_id,
        "event": "COMMENT",
        "body": body,
        "comments": comments,
    }


def _anchor_counts(findings: list[PublishedFinding]) -> dict[str, int]:
    tally = {"inline": 0, "summary": 0}
    for f in findings:
        if f.anchored in tally:
            tally[f.anchored] += 1
    return tally


def _finish(
    pr_url: str,
    owner: str,
    repo: str,
    number: int,
    head_sha: str,
    dry_run: bool,
    payload: dict,
    grounding: dict[str, int],
    anchoring: dict[str, int],
    findings: list[PublishedFinding],
    prompt_sha: str,
    model: str,
    tokens_in: int,
    tokens_out: int,
    t0: float,
    temp_dir_removed: bool,
    token: str,
    error: str | None,
    trace: list,
) -> ReviewResult:
    posted = False
    if not dry_run:
        github.post_review(owner, repo, number, token, payload)
        posted = True
        for f in findings:
            if f.anchored != "dropped":
                f.posted = True
    result = ReviewResult(
        pr_url=pr_url,
        owner=owner,
        repo=repo,
        number=number,
        head_sha=head_sha,
        dry_run=dry_run,
        posted=posted,
        payload=payload,
        grounding=grounding,
        anchoring=anchoring,
        findings=findings,
        prompt_sha=prompt_sha,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        wall_clock_s=round(time.monotonic() - t0, 3),
        temp_dir_removed=temp_dir_removed,
        error=error,
    )
    _write_trace(result, trace)
    return result


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
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"wrote {out}")


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    return text.replace(token, "***")


def _rmtree(path: str) -> None:
    def onerror(func, p, _exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)
    if os.path.exists(path):
        raise RuntimeError(f"temp dir still present: {path}")
