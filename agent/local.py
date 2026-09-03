"""Review a diff against a directory on disk. No GitHub, no network.

This is what the evals run against. A live PR cannot produce a trustworthy
number when the repo under review contains the eval harness that scores it -
the agent can read its own answer key, and did.

Same graph, same prompt, same grounding, same anchoring. Only the source of
the diff and the checkout differ.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from core.models import PublishedFinding, ReviewResult
from gh import build_review
from .graph import RECURSION_LIMIT, build_graph
from .graph_state import ReviewState
from .runtime import trace_holder


def review_local(repo_dir: str, diff: str, *, run_id: str = "local") -> ReviewResult:
    trace: list = []
    tok = trace_holder.set(trace)
    t0 = time.monotonic()
    # Copy so a tool cannot mutate the fixture, and so the jail has a real
    # boundary to enforce rather than the developer's whole home directory.
    tmp = tempfile.mkdtemp(prefix="auto-pr-local-")
    workspace = str(Path(tmp) / "repo")
    try:
        shutil.copytree(repo_dir, workspace)
        with SqliteSaver.from_conn_string(":memory:") as cp:
            app = build_graph().compile(checkpointer=cp)
            state: ReviewState = app.invoke(
                {
                    "run_id": run_id,
                    "owner": "local",
                    "repo": "fixture",
                    "number": 0,
                    "dry_run": True,
                    "head_sha": "local",
                    "diff": diff,
                    "workspace": workspace,
                    "corpus": [{"source": "diff", "text": diff}],
                    "status": "running",
                    "started_at": t0,
                    "iterations": 0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "messages": [],
                    "findings": [],
                    "budget_breach": None,
                    "error": None,
                    "posted": False,
                },
                config={
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": RECURSION_LIMIT,
                },
            )
        published, payload, tally = build_review(
            list(state.get("findings") or []), diff, "local", state.get("summary") or ""
        )
        status = state.get("status") or "running"
        if status == "running":
            status = "published"
        findings = []
        for i in published:
            try:
                findings.append(
                    PublishedFinding(
                        severity=i.get("severity") or "nit",
                        category=i.get("category") or "maintainability",
                        path=i.get("path") or i.get("file") or "",
                        line=i.get("line"),
                        title=i.get("title") or "",
                        body=i.get("body") or "",
                        evidence=i.get("evidence") or "",
                        verdict=i.get("verdict") or "ungrounded",
                        anchored=i.get("anchored") or "dropped",
                    )
                )
            except Exception:
                continue
        return ReviewResult(
            pr_url="local://fixture",
            owner="local",
            repo="fixture",
            number=0,
            head_sha="local",
            dry_run=True,
            posted=False,
            payload=payload,
            grounding=state.get("grounding") or {},
            anchoring=tally,
            findings=findings,
            prompt_sha=state.get("prompt_sha") or "",
            model="",
            tokens_in=int(state.get("tokens_in") or 0),
            tokens_out=int(state.get("tokens_out") or 0),
            wall_clock_s=round(time.monotonic() - t0, 3),
            temp_dir_removed=True,
            status=status,
            error=state.get("error"),
            corpus=list(state.get("corpus") or []),
            trace=trace,
        )
    finally:
        trace_holder.reset(tok)
        shutil.rmtree(tmp, ignore_errors=True)
