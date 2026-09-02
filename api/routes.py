import json
import uuid

from fastapi import APIRouter, HTTPException, Query

from gh.urls import parse_pr_url
from storage.db import connect, sqlite_version
from storage.runs import (
    findings_for,
    get_run,
    insert_queued,
    list_runs,
    row_to_dict,
    set_cancel,
    state_counts,
)

from .schemas import (
    CancelOut,
    FindingOut,
    Healthz,
    ReviewAccepted,
    ReviewRequest,
    RunDetail,
    RunSummary,
    TraceOut,
)

router = APIRouter()


def _summary(row) -> RunSummary:
    d = row_to_dict(row)
    return RunSummary(
        id=d["id"],
        pr_url=d["pr_url"],
        owner=d["owner"],
        repo=d["repo"],
        pr_number=d["pr_number"],
        head_sha=d.get("head_sha"),
        dry_run=bool(d.get("dry_run")),
        state=d["state"],
        attempts=d.get("attempts") or 0,
        error=d.get("error"),
        prompt_sha=d.get("prompt_sha"),
        model=d.get("model"),
        tokens_in=d.get("tokens_in"),
        tokens_out=d.get("tokens_out"),
        wall_clock_s=d.get("wall_clock_s"),
        grounded=d.get("grounded"),
        near=d.get("near"),
        ungrounded=d.get("ungrounded"),
        inline=d.get("inline"),
        summary=d.get("summary"),
        dropped=d.get("dropped"),
        created_at=d["created_at"],
        finished_at=d.get("finished_at"),
    )


@router.post("/api/review", response_model=ReviewAccepted, status_code=202)
def create_review(body: ReviewRequest) -> ReviewAccepted:
    try:
        owner, repo, number = parse_pr_url(body.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    run_id = str(uuid.uuid4())
    conn = connect()
    try:
        insert_queued(
            conn,
            run_id=run_id,
            pr_url=body.pr_url,
            owner=owner,
            repo=repo,
            pr_number=number,
            dry_run=body.dry_run,
        )
    finally:
        conn.close()
    return ReviewAccepted(run_id=run_id)


@router.get("/api/runs", response_model=list[RunSummary])
def get_runs(
    limit: int = Query(50, ge=1, le=200),
    cursor: float | None = None,
) -> list[RunSummary]:
    conn = connect()
    try:
        rows = list_runs(conn, limit=limit, cursor=cursor)
        return [_summary(r) for r in rows]
    finally:
        conn.close()


@router.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run_detail(run_id: str) -> RunDetail:
    conn = connect()
    try:
        row = get_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        found = findings_for(conn, run_id)
        base = _summary(row)
        return RunDetail(
            **base.model_dump(),
            findings=[
                FindingOut(
                    id=f["id"],
                    severity=f["severity"],
                    category=f["category"],
                    path=f["path"],
                    line=f["line"],
                    title=f["title"],
                    body=f["body"],
                    evidence=f["evidence"],
                    verdict=f["verdict"],
                    anchored=f["anchored"],
                    posted=bool(f["posted"]),
                )
                for f in found
            ],
        )
    finally:
        conn.close()


@router.get("/api/runs/{run_id}/trace", response_model=TraceOut)
def get_trace(run_id: str) -> TraceOut:
    conn = connect()
    try:
        row = get_run(conn, run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        corpus = json.loads(row["corpus_json"]) if row["corpus_json"] else None
        trace = json.loads(row["trace_json"]) if row["trace_json"] else None
        return TraceOut(run_id=run_id, corpus=corpus, trace=trace)
    finally:
        conn.close()


@router.post("/api/runs/{run_id}/cancel", response_model=CancelOut)
def cancel_run(run_id: str) -> CancelOut:
    conn = connect()
    try:
        if get_run(conn, run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        set_cancel(conn, run_id)
        return CancelOut(id=run_id, cancel=True)
    finally:
        conn.close()


@router.get("/healthz", response_model=Healthz)
def healthz() -> Healthz:
    conn = connect()
    try:
        counts = state_counts(conn)
        return Healthz(
            db="ok",
            sqlite_version=sqlite_version(conn),
            queued=counts.get("queued", 0),
            running=counts.get("running", 0),
        )
    finally:
        conn.close()
