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
    scope: dict[str, str]        # path -> added|modified|deleted
    # path -> post-image line ranges the diff touches. Lets `read_file` open a
    # file too large to return whole around its change instead of from line 1.
    hunks: dict[str, list[tuple[int, int]]]

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
    # Consecutive turns whose every tool result taught the model nothing -
    # an error, a refusal, a zero-hit search or a repeat of a call already
    # answered. The fuse for a loop that is going nowhere cheaply.
    unproductive_streak: int

    # completion
    completed: bool              # submit_findings was accepted
    submit_failed: str | None    # the forced submit could not produce findings

    # outcome
    status: str
    error: str | None
    posted: bool
