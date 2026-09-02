from pydantic import BaseModel, Field


class ReviewRequest(BaseModel):
    pr_url: str
    dry_run: bool = False


class ReviewAccepted(BaseModel):
    run_id: str


class RunSummary(BaseModel):
    id: str
    pr_url: str
    owner: str
    repo: str
    pr_number: int
    head_sha: str | None
    dry_run: bool
    state: str
    attempts: int
    error: str | None
    prompt_sha: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    wall_clock_s: float | None
    grounded: int | None
    near: int | None
    ungrounded: int | None
    inline: int | None
    summary: int | None
    dropped: int | None
    created_at: float
    finished_at: float | None


class FindingOut(BaseModel):
    id: str
    severity: str
    category: str
    path: str
    line: int | None
    title: str
    body: str
    evidence: str
    verdict: str
    anchored: str
    posted: bool


class RunDetail(RunSummary):
    findings: list[FindingOut] = Field(default_factory=list)


class TraceOut(BaseModel):
    run_id: str
    corpus: list | None
    trace: list | None


class CancelOut(BaseModel):
    id: str
    cancel: bool


class Healthz(BaseModel):
    db: str
    sqlite_version: str
    queued: int
    running: int
