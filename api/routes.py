import json

from fastapi import APIRouter, HTTPException, Query, Request, Response

from config import get_settings
from gh.urls import parse_pr_url
from gh.webhook import HEADER, should_review, verify
from storage import runs as R
from storage.db import health

from .schemas import (
    CancelOut,
    FindingOut,
    ReviewAccepted,
    ReviewRequest,
    RunDetail,
    RunSummary,
    TraceOut,
)

router = APIRouter()


# --------------------------------------------------------------------------
# Webhook. This is the production entry point.
# --------------------------------------------------------------------------
@router.post("/webhook")
async def webhook(request: Request) -> Response:
    """Verify, enqueue, return. This handler must NEVER call the model.

    GitHub retries anything slower than ~10s and an agent run takes minutes,
    so the only work here is a signature check and one INSERT.
    """
    s = get_settings()
    body = await request.body()
    if not verify(body, request.headers.get(HEADER),
                  s.github_webhook_secret.get_secret_value()):
        raise HTTPException(status_code=401, detail="bad signature")

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery")
    payload = json.loads(body)

    if event == "installation":
        inst = payload.get("installation") or {}
        acct = inst.get("account") or {}
        if payload.get("action") in ("created", "unsuspend"):
            R.upsert_installation(
                gh_installation_id=inst.get("id"),
                login=acct.get("login", ""),
                account_type=acct.get("type"),
            )
        elif payload.get("action") in ("deleted", "suspend"):
            R.suspend_installation(inst.get("id"))
        return Response(status_code=202)

    if not should_review(event, payload):
        return Response(status_code=202)   # ack and ignore; never 4xx a webhook
                                           # we simply don't care about

    pr = payload["pull_request"]
    repo = payload["repository"]
    owner = repo["owner"]["login"]
    name = repo["name"]
    number = pr["number"]
    head_sha = (pr.get("head") or {}).get("sha")

    # Coalesce BEFORE inserting: kill stale work for this PR, then queue the
    # new head. A branch pushed five times gets reviewed once.
    R.coalesce_pr(owner, name, number, head_sha)

    run_id = R.insert_queued(
        pr_url=pr["html_url"],
        owner=owner,
        repo=name,
        pr_number=number,
        installation_id=(payload.get("installation") or {}).get("id"),
        delivery_id=delivery,
        head_sha=head_sha,
    )
    if run_id is None:
        return Response(status_code=202)   # redelivery; already accepted
    return Response(content=json.dumps({"run_id": run_id}),
                    media_type="application/json", status_code=202)


# --------------------------------------------------------------------------
# Operator / debugging API
# --------------------------------------------------------------------------
def _summary(d: dict) -> RunSummary:
    return RunSummary(**{k: d.get(k) for k in RunSummary.model_fields})


@router.post("/api/review", response_model=ReviewAccepted, status_code=202)
def create_review(body: ReviewRequest) -> ReviewAccepted:
    try:
        owner, repo, number = parse_pr_url(body.pr_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    run_id = R.insert_queued(
        pr_url=body.pr_url, owner=owner, repo=repo, pr_number=number,
        dry_run=body.dry_run,
    )
    return ReviewAccepted(run_id=run_id)


@router.get("/api/runs", response_model=list[RunSummary])
def get_runs(limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
    return [_summary(r) for r in R.list_runs(limit=limit, cursor=cursor)]


@router.get("/api/runs/{run_id}", response_model=RunDetail)
def get_run_detail(run_id: str) -> RunDetail:
    row = R.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunDetail(
        **_summary(row).model_dump(),
        findings=[
            FindingOut(**{k: f.get(k) for k in FindingOut.model_fields})
            for f in R.findings_for(run_id)
        ],
    )


@router.get("/api/runs/{run_id}/trace", response_model=TraceOut)
def get_trace(run_id: str) -> TraceOut:
    row = R.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return TraceOut(run_id=run_id, corpus=row.get("corpus"), trace=row.get("trace"))


@router.post("/api/runs/{run_id}/cancel", response_model=CancelOut)
def cancel_run(run_id: str) -> CancelOut:
    if not R.set_cancel(run_id):
        raise HTTPException(status_code=404, detail="run not found")
    return CancelOut(id=run_id, cancel=True)


@router.get("/healthz")
def healthz() -> dict:
    return health()
