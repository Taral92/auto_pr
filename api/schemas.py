from datetime import datetime

from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    pr_url: str
    dry_run: bool = False


class ReviewAccepted(BaseModel):
    run_id: str | None


class RunSummary(BaseModel):
    id: str
    pr_url: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str | None = None
    dry_run: bool = False
    state: str
    attempts: int = 0
    error: str | None = None
    installation_id: int | None = None
    prompt_sha: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    wall_clock_s: float | None = None
    grounded: int | None = None
    near: int | None = None
    ungrounded: int | None = None
    inline: int | None = None
    summary: int | None = None
    dropped: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class FindingOut(BaseModel):
    id: str
    severity: str
    category: str
    path: str
    line: int | None = None
    title: str
    body: str
    evidence: str
    verdict: str
    anchored: str
    posted: bool = False


class RunDetail(RunSummary):
    findings: list[FindingOut] = Field(default_factory=list)


class TraceOut(BaseModel):
    run_id: str
    corpus: list | None = None
    trace: list | None = None


class CancelOut(BaseModel):
    id: str
    cancel: bool
