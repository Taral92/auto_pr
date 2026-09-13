"""A retry must not inherit the previous attempt's work.

`review_pr` keys the checkpointer on `thread_id = run_id`, and the worker
reuses one `run_id` for every attempt of a run. LangGraph merges the invoke
input into whatever that thread already holds, so any field the input did not
name survived the attempt that failed.

`tool_bytes` was the damaging one. `agent_step` reads the budget BEFORE
`execute_tools` recomputes it from the fresh corpus, so a run that spent its
tool-byte budget and then hit a 429 came back, breached on turn 1 against an
empty transcript, and published a degraded review of a PR it had not read.

The fix keeps the thread id - crash-recovery lineage is deliberate - and names
every attempt-local field in `fresh_state`. These tests drive the REAL graph
through a REAL checkpointer across two attempts on one thread.
"""

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agent import budget as budgets
from agent import nodes
from agent.graph import RECURSION_LIMIT, build_graph
from agent.graph_state import ReviewState, fresh_state
from agent.model_client import _Response
from agent.tools import SUBMIT_FINDINGS

RUN_ID = "run-under-retry"

DIFF = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,2 @@
 def handler():
-    return 1
+    return 2
"""

SUBMISSION = {"summary": "Nothing found.", "findings": []}


def _resp(content, stop="tool_use", in_tok=5, out_tok=3):
    return _Response(
        {
            "stop_reason": stop,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
            "content": content,
        }
    )


READ = [{"type": "tool_use", "id": "r1", "name": "read_file",
         "input": {"path": "a.py"}}]
SUBMIT = [{"type": "tool_use", "id": "s1", "name": SUBMIT_FINDINGS,
           "input": SUBMISSION}]


@pytest.fixture
def repo(tmp_path):
    # Big enough that reading it puts a visible number in tool_bytes.
    (tmp_path / "a.py").write_text("def handler():\n    return 2\n" + "# pad\n" * 400)
    return tmp_path


@pytest.fixture
def graph(tmp_path):
    """The real graph on a real on-disk checkpointer, as review_pr uses it."""
    with SqliteSaver.from_conn_string(str(tmp_path / "cp.db")) as saver:
        saver.setup()
        yield build_graph().compile(checkpointer=saver)


@pytest.fixture
def budget_spy(monkeypatch):
    """Every `tool_bytes` the graph weighed, in order."""
    seen: list[int] = []
    real = budgets.from_state

    def spy(state, settings, **kw):
        seen.append(int(state.get("tool_bytes") or 0))
        return real(state, settings, **kw)

    monkeypatch.setattr(budgets, "from_state", spy)
    return seen


def _attempt(graph, repo, monkeypatch, script):
    """One review attempt on the shared thread, exactly as review_pr builds it.

    `script` is the model's turns; an exception in it blows up mid-graph the
    way a transient failure does.
    """
    turns = iter(script)

    def fake_call(**_):
        item = next(turns)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(nodes, "_call_model", fake_call)
    return graph.invoke(
        fresh_state(
            run_id=RUN_ID,
            owner="o",
            repo="r",
            number=1,
            dry_run=True,
            head_sha="a" * 40,
            diff=DIFF,
            workspace=str(repo),
            corpus=[{"source": "diff", "text": DIFF}],
            started_at=0.0,
        ),
        config={
            "configurable": {"thread_id": RUN_ID},
            "recursion_limit": RECURSION_LIMIT,
        },
    )


# -- 1. stale tool_bytes cannot be inherited ------------------------------


def test_a_crashed_attempt_really_does_leave_tool_bytes_behind(graph, repo,
                                                               monkeypatch):
    """The hazard is real: prove the checkpoint holds the dead attempt's bytes.

    Without this, the test below could pass because nothing was ever written.
    """
    with pytest.raises(RuntimeError, match="429"):
        _attempt(graph, repo, monkeypatch,
                 [_resp(READ), RuntimeError("429 mid-run")])

    left = graph.get_state(
        {"configurable": {"thread_id": RUN_ID}}
    ).values.get("tool_bytes")
    assert left and left > 0


def test_a_retry_does_not_inherit_stale_tool_bytes(graph, repo, monkeypatch,
                                                   budget_spy):
    """The regression. Turn 1 of the retry must weigh 0 spent tool bytes."""
    with pytest.raises(RuntimeError):
        _attempt(graph, repo, monkeypatch,
                 [_resp(READ), RuntimeError("429 mid-run")])
    spent_before = list(budget_spy)
    assert max(spent_before) > 0            # attempt 1 really did spend bytes

    budget_spy.clear()
    _attempt(graph, repo, monkeypatch, [_resp(SUBMIT)])

    assert budget_spy, "the retry weighed no budget at all"
    assert budget_spy[0] == 0


def test_a_retry_after_an_exhausted_attempt_is_not_forced_to_submit(
    graph, repo, monkeypatch
):
    """The user-visible symptom: a degraded, empty review of an unread PR."""
    monkeypatch.setattr(nodes.get_settings(), "max_tool_bytes_total", 500,
                        raising=False)
    with pytest.raises(RuntimeError):
        _attempt(graph, repo, monkeypatch,
                 [_resp(READ), RuntimeError("429 mid-run")])

    final = _attempt(graph, repo, monkeypatch, [_resp(SUBMIT)])

    assert final.get("budget_breach") is None
    assert final.get("status") != "degraded"
    assert final.get("completed") is True


# -- 2. the other attempt-local fields ------------------------------------


STALE = {
    "tool_bytes": 999_999,
    "summary": "stale summary from a dead attempt",
    "grounding": {"grounded": 7},
    "anchoring": {"inline": 7},
    "payload": {"body": "stale payload"},
    "raw_output": "stale raw output",
    "stop_reason": "stale_stop",
    "diff_bytes": 123_456,
}


@pytest.mark.parametrize("field", sorted(STALE))
def test_fresh_state_resets_every_attempt_local_field(field):
    assert fresh_state()[field] == fresh_state()[field]
    assert fresh_state()[field] not in (STALE[field],)


def test_a_retry_inherits_none_of_the_stale_fields(graph, repo, monkeypatch):
    """Seed a thread with every stale value at once, then run a clean attempt."""
    graph.update_state({"configurable": {"thread_id": RUN_ID}}, dict(STALE))
    seeded = graph.get_state({"configurable": {"thread_id": RUN_ID}}).values
    assert seeded["summary"] == STALE["summary"]      # the seed took

    final = _attempt(graph, repo, monkeypatch, [_resp(SUBMIT)])

    for field, stale in STALE.items():
        assert final.get(field) != stale, f"{field} survived the retry"


# -- 3. what MUST survive is not wiped ------------------------------------


def test_the_callers_values_win_over_the_resets():
    state = fresh_state(run_id="r", owner="o", repo="p", number=9,
                        diff=DIFF, workspace="/w", started_at=12.5,
                        dry_run=True, head_sha="a" * 40,
                        corpus=[{"source": "diff", "text": DIFF}])
    assert state["run_id"] == "r"
    assert state["number"] == 9
    assert state["diff"] == DIFF
    assert state["workspace"] == "/w"
    assert state["started_at"] == 12.5
    assert state["dry_run"] is True
    assert state["corpus"] == [{"source": "diff", "text": DIFF}]


def test_the_retry_keeps_the_runs_checkpoint_lineage(graph, repo, monkeypatch):
    """Crash recovery is preserved: one run is still one thread, and the
    history of the failed attempt is still there to inspect."""
    cfg = {"configurable": {"thread_id": RUN_ID}}
    with pytest.raises(RuntimeError):
        _attempt(graph, repo, monkeypatch,
                 [_resp(READ), RuntimeError("429 mid-run")])
    after_one = len(list(graph.get_state_history(cfg)))
    assert after_one > 0

    _attempt(graph, repo, monkeypatch, [_resp(SUBMIT)])

    assert len(list(graph.get_state_history(cfg))) > after_one
    assert graph.get_state(cfg).config["configurable"]["thread_id"] == RUN_ID


def test_two_attempts_never_share_a_container():
    """Mutable resets are built per call, not once at import."""
    a, b = fresh_state(), fresh_state()
    a["messages"].append({"role": "user"})
    a["corpus"].append({"source": "x"})
    a["scope"]["p.py"] = "modified"
    assert b["messages"] == [] and b["corpus"] == [] and b["scope"] == {}


# -- 4. the schema and the reset cannot drift -----------------------------


def test_fresh_state_covers_every_field_in_the_schema():
    """The guard that keeps this fixed. A field added to ReviewState and not
    to fresh_state is the next stale value, and it fails here instead."""
    assert set(fresh_state()) == set(ReviewState.__annotations__)


def test_both_call_sites_pass_only_real_state_fields():
    import inspect

    from agent import local, review

    fields = set(ReviewState.__annotations__)
    for source in (inspect.getsource(review.review_pr),
                   inspect.getsource(local.review_local)):
        body = source.split("fresh_state(", 1)[1].split("),", 1)[0]
        passed = {
            line.split("=", 1)[0].strip()
            for line in body.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        assert passed and passed <= fields, passed - fields


# -- 5. a clean run is unchanged ------------------------------------------


def test_a_first_attempt_is_unaffected(graph, repo, monkeypatch, budget_spy):
    final = _attempt(graph, repo, monkeypatch, [_resp(READ), _resp(SUBMIT)])

    assert budget_spy[0] == 0
    assert final["completed"] is True
    assert final["summary"] == SUBMISSION["summary"]
    assert final["iterations"] == 2
    assert final["tool_bytes"] > 0          # the real read still counted
    assert final.get("budget_breach") is None
