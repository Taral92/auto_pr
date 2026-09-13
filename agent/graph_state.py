from typing import Any, TypedDict


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
    stop_reason: str | None

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


def fresh_state(**run: Any) -> ReviewState:
    """The starting state of ONE review attempt, with `run` overlaid.

    Every field of `ReviewState` is named below. That is the point, and it is
    what the accompanying test enforces: a field added to the schema and not
    added here fails that test rather than becoming the next stale value.

    The hazard this closes: `review_pr` keys the checkpointer on `thread_id =
    run_id`, and the worker reuses one `run_id` for every attempt of a run. Any
    field the invoke input did NOT name therefore survived from the attempt
    that failed. `tool_bytes` was the damaging one - `agent_step` reads the
    budget before `execute_tools` recomputes it from the fresh corpus, so a run
    that spent its tool-byte budget and then hit a 429 came back, breached on
    turn 1 against an empty transcript, and published a degraded review of a
    pull request it had not read.

    Keeping the thread id is deliberate: the checkpoint lineage for a run stays
    in one thread and LangGraph's own within-invoke resumption is untouched.
    What a retry must not inherit is the previous attempt's WORK, and a retry
    inherits none of it, because every attempt-local field is reset here.

    Containers are built per call, so two attempts never share a list or dict.
    """
    state: ReviewState = {
        # input - the caller supplies all of these
        "run_id": "",
        "owner": "",
        "repo": "",
        "number": 0,
        "dry_run": False,
        # fetched
        "head_sha": "",
        "diff": "",
        "diff_bytes": 0,
        # workspace
        "workspace": "",
        # context - assemble_context rewrites these on every attempt
        "system_prompt": "",
        "prompt_sha": "",
        "scope": {},
        "hunks": {},
        # agent loop
        "messages": [],
        "corpus": [],
        "iterations": 0,
        "raw_output": "",
        "stop_reason": None,
        # post-processing
        "findings": [],
        "summary": "",
        "grounding": {},
        "anchoring": {},
        "payload": {},
        # budgets
        "tokens_in": 0,
        "tokens_out": 0,
        "started_at": 0.0,
        "tool_bytes": 0,
        "budget_breach": None,
        "unproductive_streak": 0,
        # completion
        "completed": False,
        "submit_failed": None,
        # outcome
        "status": "running",
        "error": None,
        "posted": False,
    }
    state.update(run)
    return state
