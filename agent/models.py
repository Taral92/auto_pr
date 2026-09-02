from typing import Any, Literal

from pydantic import BaseModel

Severity = Literal["blocker", "should_fix", "nit"]
Category = Literal[
    "correctness", "security", "performance", "maintainability", "test_gap"
]
Verdict = Literal["grounded", "near", "ungrounded"]
Anchored = Literal["inline", "summary", "dropped"]


class Finding(BaseModel):
    severity: Severity
    category: Category
    file: str
    title: str
    description: str
    recommendation: str
    evidence: str


class ReviewFindings(BaseModel):
    summary: str
    findings: list[Finding]


class PublishedFinding(BaseModel):
    severity: Severity
    category: Category
    path: str
    line: int | None
    title: str
    body: str
    evidence: str
    verdict: Verdict
    anchored: Anchored
    posted: bool = False


class ReviewResult(BaseModel):
    pr_url: str
    owner: str
    repo: str
    number: int
    head_sha: str
    dry_run: bool
    posted: bool
    payload: dict[str, Any]
    grounding: dict[str, int]
    anchoring: dict[str, int]
    findings: list[PublishedFinding]
    prompt_sha: str
    model: str
    tokens_in: int
    tokens_out: int
    wall_clock_s: float
    temp_dir_removed: bool
    error: str | None = None
