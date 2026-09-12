import json
import time

import pytest

from agent import budget as budgets
from agent.nodes import (
    _accept,
    _call_sig,
    _prior_tool_sigs,
    _submission,
    after_agent,
    after_forced,
    after_tools,
    degrade,
    execute_tools,
)
from agent.tools import SUBMIT_FINDINGS
from config import get_settings


def _submit_block(payload: dict, block_id: str = "s1") -> dict:
    return {
        "type": "tool_use",
        "id": block_id,
        "name": SUBMIT_FINDINGS,
        "input": payload,
    }


def _assistant(*blocks: dict) -> dict:
    return {"role": "assistant", "content": list(blocks)}


EMPTY_SUBMISSION = {"summary": "No defects found.", "findings": []}

READ_BLOCK = {
    "type": "tool_use",
    "id": "read-1",
    "name": "read_file",
    "input": {"path": "app/files.py"},
}


# -- routing ---------------------------------------------------------------


def test_tool_block_routes_to_tool_execution():
    state = {"status": "running", "messages": [_assistant(READ_BLOCK)]}

    assert after_agent(state) == "execute_tools"


def test_submit_findings_routes_to_tool_execution_which_completes_it():
    state = {
        "status": "running",
        "messages": [_assistant(_submit_block(EMPTY_SUBMISSION))],
    }

    assert after_agent(state) == "execute_tools"


def test_text_without_a_tool_call_is_a_deviation_not_a_completion():
    """Silence used to BE the completion signal, so a model that drifted off
    protocol and a model that was finished were the same event."""
    state = {
        "status": "running",
        "messages": [_assistant({"type": "text", "text": '{"findings":[]}'})],
    }

    assert after_agent(state) == "forced_submit"


def test_a_breach_diverts_before_the_turns_tools_are_dispatched():
    """The old loop ran the final turn's tools and then degraded, so those
    reads were paid for and thrown away unread."""
    state = {
        "status": "running",
        "budget_breach": "tokens",
        "messages": [_assistant(READ_BLOCK)],
    }

    assert after_agent(state) == "forced_submit"


def test_a_submission_in_hand_outranks_a_breach():
    """Forcing another call would spend tokens to be handed the same answer."""
    state = {
        "status": "running",
        "budget_breach": "tokens",
        "messages": [_assistant(_submit_block(EMPTY_SUBMISSION))],
    }

    assert after_agent(state) == "execute_tools"


def test_failed_status_still_reaches_fail():
    state = {
        "status": "failed",
        "messages": [_assistant({"type": "text", "text": ""})],
    }

    assert after_agent(state) == "fail"


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"completed": True}, "ground"),
        ({"budget_breach": "tokens"}, "forced_submit"),
        ({}, "agent_step"),
        # A submission accepted on the same turn a budget ran out is still a
        # submission: publish it, do not pay for another call.
        ({"completed": True, "budget_breach": "tokens"}, "ground"),
    ],
)
def test_after_tools_routing(state, expected):
    assert after_tools(state) == expected


@pytest.mark.parametrize(
    "state,expected",
    [
        ({}, "ground"),
        ({"budget_breach": "turns"}, "degrade"),
        ({"submit_failed": "no submit_findings call"}, "degrade"),
    ],
)
def test_after_forced_routing(state, expected):
    assert after_forced(state) == expected


def test_every_branch_the_routers_can_take_is_a_real_node():
    """LangGraph only raises on an unmapped branch key at invoke time.

    A route added here without a node and a map entry in `graph.py` would
    otherwise surface as a crashed run in production, not a failed test.
    """
    from agent.graph import build_graph

    nodes = set(build_graph().compile().get_graph().nodes)
    routes = {
        after_agent({"status": "failed", "messages": [_assistant()]}),
        after_agent({"status": "running", "messages": [_assistant(READ_BLOCK)]}),
        after_agent({"status": "running", "messages": [_assistant()]}),
        after_agent(
            {
                "status": "running",
                "budget_breach": "tokens",
                "messages": [_assistant(READ_BLOCK)],
            }
        ),
        after_tools({"completed": True}),
        after_tools({"budget_breach": "tokens"}),
        after_tools({}),
        after_forced({}),
        after_forced({"budget_breach": "tokens"}),
    }

    assert routes == {
        "fail",
        "execute_tools",
        "forced_submit",
        "ground",
        "agent_step",
        "degrade",
    }
    assert routes <= nodes


# -- completion ------------------------------------------------------------


def test_submission_is_found_among_other_tool_calls():
    assert _submission([READ_BLOCK, _submit_block(EMPTY_SUBMISSION)]) is not None
    assert _submission([READ_BLOCK]) is None


def test_accept_validates_against_the_findings_schema():
    accepted, err = _accept(_submit_block(EMPTY_SUBMISSION))
    assert err is None
    assert accepted == {"findings": [], "summary": "No defects found."}


def test_accept_rejects_a_submission_that_does_not_match_the_schema():
    accepted, err = _accept(_submit_block({"findings": "not a list"}))
    assert accepted is None
    assert err


def _tool_state(messages, **extra):
    return {
        "workspace": "/nonexistent",
        "scope": {},
        "corpus": [],
        "iterations": 1,
        "started_at": time.monotonic(),
        "tokens_in": 0,
        "tokens_out": 0,
        "messages": messages,
        **extra,
    }


def test_execute_tools_completes_the_run_on_a_valid_submission():
    out = execute_tools(_tool_state([_assistant(_submit_block(EMPTY_SUBMISSION))]))

    assert out["completed"] is True
    assert out["summary"] == "No defects found."
    assert out["findings"] == []


def test_a_submission_suppresses_the_other_tool_calls_in_its_turn():
    """Those results would be dispatched into a conversation that has ended."""
    out = execute_tools(
        _tool_state([_assistant(READ_BLOCK, _submit_block(EMPTY_SUBMISSION))])
    )

    assert out["completed"] is True
    assert out["corpus"] == []          # read_file never ran
    bodies = [b.get("content") for b in out["messages"][-1]["content"]]
    assert any("not run" in str(b) for b in bodies)


def test_an_invalid_submission_does_not_complete_the_run():
    out = execute_tools(
        _tool_state([_assistant(_submit_block({"findings": "not a list"}))])
    )

    assert not out.get("completed")
    body = str(out["messages"][-1]["content"][0]["content"])
    assert "submit_findings rejected" in body


def test_degrade_reports_which_budget_went():
    assert degrade({"budget_breach": "tokens"})["error"] == "budget_breach:tokens"


def test_degrade_reports_a_failed_submission():
    out = degrade({"submit_failed": "forced submit returned no call"})
    assert out["status"] == "degraded"
    assert out["error"].startswith("submit_failed:")


# -- the budget the model reads -------------------------------------------


def test_every_tool_turn_ends_with_the_budget():
    """A run making steady progress used to get no signal at all that it was
    on turn 8 of 10, so it could not choose to wrap up."""
    out = execute_tools(_tool_state([_assistant(READ_BLOCK)]))

    tail = out["messages"][-1]["content"][-1]
    assert tail["type"] == "text"
    assert tail["text"].startswith("[budget ")
    assert "turn 1/" in tail["text"]


def test_the_budget_line_never_enters_the_corpus():
    """Grounding matches evidence against the corpus. A budget note landing
    there could be quoted back as if it were code under review."""
    out = execute_tools(_tool_state([_assistant(READ_BLOCK)]))

    assert all("[budget" not in (c.get("text") or "") for c in out["corpus"])


def test_a_completed_turn_carries_no_budget_line():
    out = execute_tools(_tool_state([_assistant(_submit_block(EMPTY_SUBMISSION))]))

    assert all(b["type"] == "tool_result" for b in out["messages"][-1]["content"])


# -- the rolling cache breakpoint ------------------------------------------
#
# One file was 92% of all tool output in the wide-refactor recording, and it
# landed after every breakpoint, so all five later calls re-sent it at full
# price: ~40K input tokens a turn, zero cache reads. The breakpoint now
# follows the results instead of sitting in front of them.

FIRST_MESSAGE = {
    "role": "user",
    "content": [
        {
            "type": "text",
            "text": "CHANGED FILES (1):\n  a.py (modified)\n\nDIFF:\n\n...",
            "cache_control": {"type": "ephemeral"},
        }
    ],
}


def _breakpoints(messages: list) -> list[tuple[int, str]]:
    """(message index, block type) of every cache_control in the transcript."""
    found = []
    for i, message in enumerate(messages):
        for block in message.get("content") or []:
            if isinstance(block, dict) and "cache_control" in block:
                found.append((i, block["type"]))
    return found


def _read_turn(repo, block_id: str, path: str = "a.py") -> dict:
    return _assistant(
        {"type": "tool_use", "id": block_id, "name": "read_file",
         "input": {"path": path}}
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "a.py").write_text("def handler():\n    return 1\n")
    (tmp_path / "b.py").write_text("def other():\n    return 2\n")
    return tmp_path


def _turn(repo, messages, **extra):
    return execute_tools(
        _tool_state(
            messages,
            workspace=str(repo),
            scope={"a.py": "modified", "b.py": "modified"},
            **extra,
        )
    )


def test_the_breakpoint_lands_on_the_newest_tool_result(repo):
    out = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])

    assert _breakpoints(out["messages"])[-1] == (2, "tool_result")


def test_the_breakpoint_is_never_on_the_budget_line(repo):
    """That text changes every turn; a prefix is reused only while it is
    byte-identical, so a breakpoint behind it would invalidate itself."""
    out = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])

    blocks = out["messages"][-1]["content"]
    assert blocks[-1]["type"] == "text"          # the budget line is last
    assert "cache_control" not in blocks[-1]


def test_only_one_rolling_breakpoint_is_ever_in_flight(repo):
    """The request carries a hard limit of four."""
    first = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])
    second = _turn(
        repo, [*first["messages"], _read_turn(repo, "r2", "b.py")]
    )
    third = _turn(
        repo,
        [*second["messages"], _assistant(_search_block("handler", "s9"))],
    )

    for messages in (first["messages"], second["messages"], third["messages"]):
        on_results = [b for b in _breakpoints(messages) if b[1] == "tool_result"]
        assert len(on_results) == 1
        assert len(_breakpoints(messages)) <= 4


def test_the_breakpoint_moves_forward_each_turn(repo):
    first = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])
    second = _turn(repo, [*first["messages"], _read_turn(repo, "r2", "b.py")])

    assert _breakpoints(first["messages"])[-1][0] == 2
    assert _breakpoints(second["messages"])[-1][0] == 4


def test_the_static_first_message_breakpoint_is_left_alone(repo):
    """It is the prefix every rolling read builds on, and the only breakpoint
    that can fire on turn one."""
    first = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])
    second = _turn(repo, [*first["messages"], _read_turn(repo, "r2", "b.py")])

    assert (0, "text") in _breakpoints(second["messages"])


def test_clearing_the_breakpoint_does_not_edit_the_previous_state(repo):
    """These block dicts are shared with the previous state snapshot."""
    first = _turn(repo, [FIRST_MESSAGE, _read_turn(repo, "r1")])
    before = json.dumps(first["messages"], sort_keys=True)

    _turn(repo, [*first["messages"], _read_turn(repo, "r2", "b.py")])

    assert json.dumps(first["messages"], sort_keys=True) == before


def test_a_completed_turn_adds_no_breakpoint(repo):
    """Nothing is sent after a submission, so there is nothing to cache for."""
    out = _turn(repo, [FIRST_MESSAGE, _assistant(_submit_block(EMPTY_SUBMISSION))])

    assert [b for b in _breakpoints(out["messages"]) if b[1] == "tool_result"] == []


# -- what a turn costs -----------------------------------------------------


def test_usage_normalises_a_response_that_reports_no_cache():
    from agent.model_client import context_in, usage_of

    used = usage_of(_fake_response([], in_tokens=10, out_tokens=2))
    assert used == {
        "input": 10,
        "cache_read": 0,
        "cache_write": 0,
        "output": 2,
        "reasoning": 0,
    }
    assert context_in(used) == 10


def test_usage_tolerates_none_from_the_live_sdk():
    from agent.model_client import usage_of

    class _U:
        input_tokens = 5
        output_tokens = 1
        cache_read_input_tokens = None
        cache_creation_input_tokens = None

    class _R:
        usage = _U()

    assert usage_of(_R())["cache_read"] == 0


def test_cached_input_still_counts_against_the_budget():
    """Billed at a fraction, but it occupies the window and is re-sent every
    turn. Leaving it out would let a working cache disable the token budget."""
    from agent.model_client import context_in

    assert context_in(
        {"input": 100, "cache_read": 39000, "cache_write": 500, "output": 7}
    ) == 39600


def test_agent_step_counts_cache_reads_into_tokens_in(monkeypatch):
    from agent import nodes
    from agent.model_client import _Response

    def fake_call(**_):
        return _Response(
            {
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 39_000,
                    "cache_creation_input_tokens": 600,
                },
                "content": [{"type": "text", "text": "..."}],
            }
        )

    monkeypatch.setattr(nodes, "_call_model", fake_call)
    out = nodes.agent_step(
        {
            "system_prompt": "sys",
            "messages": [FIRST_MESSAGE],
            "iterations": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "started_at": time.monotonic(),
        }
    )

    assert out["tokens_in"] == 39_720        # 120 + 39,000 + 600
    assert out["tokens_out"] == 30


def test_the_trace_records_the_cache_split(monkeypatch):
    """A run that reports only `api_in` cannot show whether caching worked."""
    from agent import nodes
    from agent.model_client import _Response
    from agent.runtime import trace_holder

    monkeypatch.setattr(
        nodes,
        "_call_model",
        lambda **_: _Response(
            {
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "cache_read_input_tokens": 39_000,
                    "cache_creation_input_tokens": 600,
                },
                "content": [{"type": "text", "text": "..."}],
            }
        ),
    )
    trace: list = []
    token = trace_holder.set(trace)
    try:
        nodes.agent_step(
            {
                "system_prompt": "sys",
                "messages": [FIRST_MESSAGE],
                "iterations": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "started_at": time.monotonic(),
            }
        )
    finally:
        trace_holder.reset(token)

    assert trace[-1]["cache_read"] == 39_000
    assert trace[-1]["cache_write"] == 600
    assert trace[-1]["api_in"] == 120


def test_the_system_block_carries_no_breakpoint_of_its_own():
    """tools + system measures ~1,250 tokens and this model will not cache a
    prefix below 2,048, so that breakpoint cached nothing - silently, which is
    the only way a breakpoint can fail. The system prompt is still cached: it
    sits inside the first user message's prefix."""
    from agent import nodes
    from agent.runtime import model_client_var

    seen = {}

    class _Stub:
        def call(self, **kwargs):
            seen.update(kwargs)
            return _fake_response([{"type": "text", "text": "x"}])

    token = model_client_var.set(_Stub())
    try:
        nodes._call_model(system="SYSTEM", messages=[FIRST_MESSAGE])
    finally:
        model_client_var.reset(token)

    # A plain string: no block wrapper, so nowhere for a breakpoint to hide.
    assert seen["system"] == "SYSTEM"


# -- the no-progress fuse --------------------------------------------------


def _search_block(pattern: str, block_id: str) -> dict:
    return {
        "type": "tool_use",
        "id": block_id,
        "name": "search_code",
        "input": {"pattern": pattern},
    }


def test_a_turn_that_taught_nothing_advances_the_dead_end_streak(tmp_path):
    out = execute_tools(
        _tool_state(
            [_assistant(_search_block("nothing_matches", "s1"))],
            workspace=str(tmp_path),
            unproductive_streak=1,
        )
    )

    assert out["unproductive_streak"] == 2


def test_a_useful_turn_resets_the_streak(tmp_path):
    (tmp_path / "a.py").write_text("def handler():\n    return 1\n")
    out = execute_tools(
        _tool_state(
            [_assistant(_search_block("handler", "s1"))],
            workspace=str(tmp_path),
            scope={"a.py": "modified"},
            unproductive_streak=2,
        )
    )

    assert out["unproductive_streak"] == 0


def test_the_dead_end_streak_trips_the_fuse(tmp_path):
    """Turns that go nowhere are cheap, so no cost dimension can see them."""
    cap = get_settings().max_unproductive_turns
    out = execute_tools(
        _tool_state(
            [_assistant(_search_block("nothing_matches", "s1"))],
            workspace=str(tmp_path),
            unproductive_streak=cap - 1,
        )
    )

    assert out["budget_breach"] == "dead_ends"


def test_a_duplicate_call_counts_as_a_dead_end(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    messages = [
        _assistant(READ_BLOCK),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "read-1", "content": "..."}]},
        _assistant(dict(READ_BLOCK, id="read-2")),
    ]
    out = execute_tools(
        _tool_state(messages, workspace=str(tmp_path), unproductive_streak=0)
    )

    assert out["unproductive_streak"] == 1
    assert out["corpus"] == []          # the repeat was answered, not re-run


# -- the forced submission -------------------------------------------------
#
# This replaces `_repair`, which was invisible three ways: its tokens were
# never added to the run's totals, so the call handling a breach could exceed
# it; it was never traced, so the call that produced the output of every
# exhausted run was missing from that run's record; and it asked for JSON as
# prose, so it could fail at its only job.


def _forced_state(**extra) -> dict:
    return {
        "status": "running",
        "system_prompt": "sys",
        "messages": [_assistant(READ_BLOCK)],
        "iterations": 4,
        "tokens_in": 100,
        "tokens_out": 20,
        "budget_breach": "tokens",
        **extra,
    }


def _fake_response(content: list[dict], *, in_tokens=7, out_tokens=3):
    from agent.model_client import _Response

    return _Response(
        {
            "stop_reason": "tool_use",
            "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
            "content": content,
        }
    )


def _run_forced(monkeypatch, content, state=None):
    from agent import nodes
    from agent.runtime import trace_holder

    seen = {}

    def fake_call(**kwargs):
        seen.update(kwargs)
        # Snapshot: forced_submit appends the reply to the list it just sent,
        # so the request has to be captured before it grows a turn.
        seen["sent"] = list(kwargs["messages"])
        return _fake_response(content)

    monkeypatch.setattr(nodes, "_call_model", fake_call)
    trace: list = []
    token = trace_holder.set(trace)
    try:
        out = nodes.forced_submit(state or _forced_state())
    finally:
        trace_holder.reset(token)
    return out, seen, trace


def test_the_forced_call_constrains_the_model_to_submit_findings(monkeypatch):
    _, seen, _ = _run_forced(monkeypatch, [_submit_block(EMPTY_SUBMISSION)])

    assert seen["tool_choice"] == {"type": "tool", "name": SUBMIT_FINDINGS}
    assert [t["name"] for t in seen["tools"]] == [SUBMIT_FINDINGS]


def test_the_forced_call_is_counted_against_the_run(monkeypatch):
    out, _, _ = _run_forced(monkeypatch, [_submit_block(EMPTY_SUBMISSION)])

    assert out["tokens_in"] == 107          # 100 + 7
    assert out["tokens_out"] == 23          # 20 + 3
    assert out["iterations"] == 5


def test_the_forced_call_is_traced(monkeypatch):
    _, _, trace = _run_forced(monkeypatch, [_submit_block(EMPTY_SUBMISSION)])

    assert trace[-1]["forced_submit"] == "tokens"
    assert trace[-1]["api_in"] == 7


def test_the_forced_call_tells_the_model_which_budget_went(monkeypatch):
    _, seen, _ = _run_forced(monkeypatch, [_submit_block(EMPTY_SUBMISSION)])

    assert "token budget" in seen["sent"][-1]["content"]


def test_a_forced_submission_completes_the_run(monkeypatch):
    out, _, _ = _run_forced(monkeypatch, [_submit_block(EMPTY_SUBMISSION)])

    assert out["completed"] is True
    assert out["summary"] == "No defects found."
    assert not out.get("submit_failed")


def test_a_forced_call_that_submits_nothing_degrades_rather_than_crashing(monkeypatch):
    out, _, _ = _run_forced(monkeypatch, [{"type": "text", "text": "sorry"}])

    assert out["submit_failed"]
    assert out["findings"] == []
    assert after_forced(out) == "degrade"


def test_a_forced_submission_that_fails_validation_degrades(monkeypatch):
    out, _, _ = _run_forced(monkeypatch, [_submit_block({"findings": "nope"})])

    assert out["submit_failed"].startswith("forced submit was invalid")
    assert after_forced(out) == "degrade"


# -- the Budget object -----------------------------------------------------


def _budget_state(**extra) -> dict:
    return {"started_at": 1000.0, "tokens_in": 0, "tokens_out": 0, **extra}


def test_tokens_are_the_binding_constraint_when_they_are_the_most_spent():
    s = get_settings()
    b = budgets.from_state(
        _budget_state(tokens_in=s.max_tokens_total // 2), s, now=1000.0
    )
    assert b.fraction == pytest.approx(0.5, abs=0.01)
    assert b.breach is None


def test_breach_names_the_dimension_that_is_gone():
    s = get_settings()
    b = budgets.from_state(_budget_state(tokens_in=s.max_tokens_total), s, now=1000.0)
    assert b.breach == "tokens"


def test_wall_clock_is_measured_without_sleeping_through_it():
    s = get_settings()
    b = budgets.from_state(
        _budget_state(), s, now=1000.0 + s.max_wall_clock_s
    )
    assert b.breach == "seconds"


def test_a_work_dimension_is_reported_before_a_fuse():
    """`tokens` says what happened; `turns` only says that it stopped."""
    s = get_settings()
    b = budgets.from_state(
        _budget_state(tokens_in=s.max_tokens_total, iterations=s.max_turns),
        s,
        now=1000.0,
    )
    assert b.breach == "tokens"


def test_turns_are_a_fuse_and_do_not_drive_the_reported_fraction():
    """Six reads in one turn spent 18% of the tokens and a tenth of the turns.
    A fuse driving the percentage would report that run as nearly finished."""
    s = get_settings()
    b = budgets.from_state(_budget_state(iterations=s.max_turns - 1), s, now=1000.0)
    assert b.fraction == 0.0
    assert b.breach is None


def test_the_turn_fuse_still_trips():
    s = get_settings()
    b = budgets.from_state(_budget_state(iterations=s.max_turns), s, now=1000.0)
    assert b.breach == "turns"


def test_the_rendered_line_escalates_as_the_budget_goes():
    s = get_settings()
    low = budgets.from_state(_budget_state(), s, now=1000.0).render()
    mid = budgets.from_state(
        _budget_state(tokens_in=int(s.max_tokens_total * 0.6)), s, now=1000.0
    ).render()
    high = budgets.from_state(
        _budget_state(tokens_in=int(s.max_tokens_total * 0.9)), s, now=1000.0
    ).render()

    assert "as soon as you have enough evidence" in low
    assert "Prefer reading changed files" in mid
    assert "Finish now" in high
    assert high.startswith("[budget 90% used - ")


# -- unchanged machinery the fuse now depends on ---------------------------


def test_call_sig_is_argument_order_independent():
    a = _call_sig("search_code", {"pattern": "x", "path": "."})
    b = _call_sig("search_code", {"path": ".", "pattern": "x"})
    assert a == b


def test_prior_sigs_excludes_the_current_turn():
    """The last message is the turn we are about to execute, not a prior call.

    A file read once and requested again must be recognised as a repeat, while
    the request under execution is not counted against itself.
    """
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "1", "name": "read_file",
             "input": {"path": "a.py"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "1", "content": "..."}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "2", "name": "read_file",
             "input": {"path": "a.py"}}]},
    ]
    seen = _prior_tool_sigs(messages)
    assert _call_sig("read_file", {"path": "a.py"}) in seen
    assert _call_sig("read_file", {"path": "b.py"}) not in seen
