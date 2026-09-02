from typing import TypedDict


class ReviewState(TypedDict, total=False):
    # input
    run_id: str
    owner: str
    repo: str
    number: int
    dry_run: bool

    # fetched
    head_sha: str
    diff: str
    diff_bytes: int

    # workspace
    workspace: str

    # context
    system_prompt: str
    prompt_sha: str

    # agent loop
    messages: list[dict]
    corpus: list[dict]
    iterations: int
    raw_output: str
    stop_reason: str

    # post-processing
    findings: list[dict]
    summary: str
    grounding: dict
    anchoring: dict
    payload: dict

    # budgets
    tokens_in: int
    tokens_out: int
    started_at: float
    tool_bytes: int
    budget_breach: str | None

    # outcome
    status: str
    error: str | None
    posted: bool
