"""The OpenAI provider boundary.

Everything above `agent/model_client.py` speaks one canonical format - the
graph, the budgets, the fuses, submit_findings, grounding, the change map.
OpenAI is translated INTO that format rather than the format being ported to
OpenAI, which is why Phases 1-4 and their tests are untouched by this
migration. These tests pin the translation, and above all they pin the token
accounting, where the two providers disagree in a way that fails silently.
"""

import json

import pytest

from agent.model_client import (
    _openai_canonical,
    _openai_input,
    _openai_tools,
    _openai_usage,
    context_in,
    inline_defs,
    strictify,
    usage_of,
)
from agent.tools import SUBMIT_SCHEMA, TOOL_SCHEMAS
from core.models import ReviewFindings


# -- fakes shaped like the Responses SDK objects ---------------------------


class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _usage(inp, out, cached=0, written=0, reasoning=0):
    return _Obj(
        input_tokens=inp,
        output_tokens=out,
        input_tokens_details=_Obj(cached_tokens=cached, cache_write_tokens=written),
        output_tokens_details=_Obj(reasoning_tokens=reasoning),
    )


def _call(name, args, call_id="call_1"):
    return _Obj(type="function_call", call_id=call_id, id="fc_1", name=name,
                arguments=args)


def _message(text):
    return _Obj(type="message", content=[_Obj(text=text)])


def _resp(output, usage=None, status="completed", reason=None):
    return _Obj(
        output=output,
        usage=usage or _usage(10, 2),
        status=status,
        incomplete_details=_Obj(reason=reason) if reason else None,
    )


# -- token accounting: the one that fails silently -------------------------
#
# Anthropic's input_tokens EXCLUDES cached and cache-write tokens. OpenAI's
# INCLUDES them. Summing OpenAI's raw numbers the way the Anthropic path does
# would count every cached token twice, inflate tokens_in, and fire the hard
# budget early - degrading healthy runs for no reason at all.


def test_context_in_equals_openais_own_input_tokens():
    """The whole point: no double counting, whatever the cache did."""
    resp = _resp([], _usage(inp=40_000, out=50, cached=39_000, written=600))
    assert context_in(usage_of_canonical(resp)) == 40_000


def usage_of_canonical(resp):
    from agent.model_client import _Response

    return usage_of(_Response(_openai_canonical(resp)))


def test_cached_and_written_tokens_are_subtracted_out_of_input():
    u = _openai_usage(_resp([], _usage(40_000, 50, cached=39_000, written=600)))
    assert u["input_tokens"] == 400            # 40,000 - 39,000 - 600
    assert u["cache_read_input_tokens"] == 39_000
    assert u["cache_creation_input_tokens"] == 600


def test_a_fully_cached_turn_never_reports_negative_input():
    u = _openai_usage(_resp([], _usage(1_000, 5, cached=1_000)))
    assert u["input_tokens"] == 0


def test_usage_survives_a_response_with_no_details_block():
    u = _openai_usage(_Obj(usage=_Obj(input_tokens=7, output_tokens=3)))
    assert u == {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_reasoning_tokens_are_captured():
    u = _openai_usage(_resp([], _usage(10, 900, reasoning=850)))
    assert u["reasoning_tokens"] == 850
    assert u["output_tokens"] == 900           # reasoning is inside output


def test_output_tokens_are_not_double_counted_with_reasoning():
    u = _openai_usage(_resp([], _usage(10, 900, reasoning=850)))
    assert u["output_tokens"] == 900


# -- schema flattening for strict mode -------------------------------------


def test_the_findings_schema_starts_out_unusable():
    """pydantic emits $defs/$ref, which strict mode rejects outright."""
    raw = ReviewFindings.model_json_schema()
    assert "$defs" in raw
    assert "$ref" in json.dumps(raw)


def test_inlining_removes_every_ref_and_def():
    flat = inline_defs(ReviewFindings.model_json_schema())
    text = json.dumps(flat)
    assert "$defs" not in text and "$ref" not in text


def test_inlining_preserves_the_nested_finding_fields():
    flat = inline_defs(ReviewFindings.model_json_schema())
    finding = flat["properties"]["findings"]["items"]
    assert set(finding["properties"]) == {
        "severity", "category", "file", "title",
        "description", "recommendation", "evidence",
    }


def test_strict_preconditions_hold_after_inlining():
    """additionalProperties false everywhere, and every field required."""
    flat = inline_defs(ReviewFindings.model_json_schema())

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, node
                assert set(node["properties"]) == set(node.get("required", [])), node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for item in node:
                check(item)

    check(flat)


def test_inlining_is_safe_on_a_self_referential_schema():
    schema = {
        "$defs": {"Node": {"type": "object",
                           "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        "$ref": "#/$defs/Node",
    }
    assert "$ref" not in json.dumps(inline_defs(schema))


# -- tool translation ------------------------------------------------------


def test_tools_become_responses_function_tools():
    tools = _openai_tools(TOOL_SCHEMAS)
    assert {t["type"] for t in tools} == {"function"}
    assert [t["name"] for t in tools] == [t["name"] for t in TOOL_SCHEMAS]
    assert all(t["strict"] is True for t in tools)
    assert all("$ref" not in json.dumps(t["parameters"]) for t in tools)


def test_submit_findings_keeps_its_schema_through_translation():
    submit = next(t for t in _openai_tools([SUBMIT_SCHEMA]) if t["name"] == "submit_findings")
    assert set(submit["parameters"]["properties"]) == {"summary", "findings"}


def test_no_tools_stays_none():
    assert _openai_tools(None) is None


def test_hand_written_tool_schemas_are_made_strict_compatible():
    """read_file/search_code are plain dicts in tools.py with neither
    `additionalProperties: false` nor every field required. The live API
    rejects that outright, so the boundary fixes it rather than tools.py."""
    for tool in _openai_tools(TOOL_SCHEMAS):
        params = tool["parameters"]
        assert params["additionalProperties"] is False, tool["name"]
        assert set(params["required"]) == set(params["properties"]), tool["name"]


def test_an_optional_argument_becomes_required_and_nullable():
    """`search_code.path` is optional; strict mode has no such concept."""
    search = next(t for t in _openai_tools(TOOL_SCHEMAS) if t["name"] == "search_code")
    path = search["parameters"]["properties"]["path"]

    assert "path" in search["parameters"]["required"]
    assert "null" in path["type"]


def test_a_required_argument_does_not_become_nullable():
    search = next(t for t in _openai_tools(TOOL_SCHEMAS) if t["name"] == "search_code")
    assert search["parameters"]["properties"]["pattern"]["type"] == "string"


def test_strictify_reaches_nested_objects():
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object", "properties": {"a": {"type": "string"}}}}},
        "required": ["items"],
    }
    inner = strictify(schema)["properties"]["items"]["items"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["a"]


def test_explicit_nulls_are_dropped_from_tool_arguments():
    """The tools were written against an ABSENT optional argument, not None.
    `search_code(path=None)` would sail past its default and crash."""
    out = _openai_canonical(_resp([_call("search_code", '{"pattern":"x","path":null}')]))
    assert out["content"][0]["input"] == {"pattern": "x"}


def test_a_supplied_optional_argument_survives():
    out = _openai_canonical(_resp([_call("search_code", '{"pattern":"x","path":"a.py"}')]))
    assert out["content"][0]["input"] == {"pattern": "x", "path": "a.py"}


# -- request translation ---------------------------------------------------


CACHED_FIRST = {
    "role": "user",
    "content": [{"type": "text", "text": "CHANGED FILES...",
                 "cache_control": {"type": "ephemeral"}}],
}


def test_cache_control_markers_are_stripped():
    """OpenAI has no explicit breakpoints in implicit mode. The markers stay
    in the canonical transcript for the Anthropic path and die here."""
    items = _openai_input([CACHED_FIRST])
    assert "cache_control" not in json.dumps(items)


def test_a_tool_use_block_becomes_a_function_call():
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "read_file",
         "input": {"path": "a.py"}}]}]
    item = _openai_input(messages)[0]

    assert item["type"] == "function_call"
    assert item["call_id"] == "t1"
    assert item["name"] == "read_file"
    assert json.loads(item["arguments"]) == {"path": "a.py"}


def test_a_tool_result_block_becomes_a_function_call_output():
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "file body"}]}]
    item = _openai_input(messages)[0]

    assert item == {"type": "function_call_output", "call_id": "t1",
                    "output": "file body"}


def test_the_budget_line_survives_as_its_own_item():
    """Phase 1's per-turn budget text must still reach the model."""
    messages = [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "body"},
        {"type": "text", "text": "[budget 31% used - ...]"}]}]
    items = _openai_input(messages)

    assert items[0]["type"] == "function_call_output"
    assert "[budget 31% used" in items[1]["content"]


def test_parallel_tool_calls_in_one_turn_all_translate():
    messages = [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
        {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b.py"}}]}]
    items = _openai_input(messages)

    assert [i["call_id"] for i in items] == ["t1", "t2"]


def test_a_plain_string_message_passes_through():
    """forced_submit builds its prompt as a bare string."""
    items = _openai_input([{"role": "user", "content": "report now"}])
    assert items == [{"role": "user", "content": "report now"}]


# -- response translation --------------------------------------------------


def test_a_function_call_becomes_a_tool_use_block():
    out = _openai_canonical(_resp([_call("read_file", '{"path": "a.py"}')]))
    block = out["content"][0]

    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["input"] == {"path": "a.py"}
    assert out["stop_reason"] == "tool_use"


def test_arguments_arrive_as_a_string_and_are_parsed():
    out = _openai_canonical(_resp([_call("submit_findings",
                                         '{"summary":"s","findings":[]}')]))
    assert out["content"][0]["input"] == {"summary": "s", "findings": []}


def test_malformed_arguments_do_not_crash_the_run():
    """It routes into Phase 1's existing invalid-submission path instead."""
    out = _openai_canonical(_resp([_call("submit_findings", "{not json")]))
    assert out["content"][0]["type"] == "tool_use"
    assert "__unparsed_arguments__" in out["content"][0]["input"]


def test_a_message_becomes_a_text_block():
    out = _openai_canonical(_resp([_message("thinking out loud")]))
    assert out["content"] == [{"type": "text", "text": "thinking out loud"}]
    assert out["stop_reason"] == "end_turn"


def test_incomplete_status_is_reported_as_the_stop_reason():
    out = _openai_canonical(
        _resp([_message("partial")], status="incomplete", reason="max_output_tokens")
    )
    assert out["stop_reason"] == "max_output_tokens"


def test_a_parallel_turn_round_trips_losslessly():
    canonical = _openai_canonical(
        _resp([_call("read_file", '{"path":"a.py"}', "c1"),
               _call("search_code", '{"pattern":"x"}', "c2")])
    )
    back = _openai_input([{"role": "assistant", "content": canonical["content"]}])

    assert [i["call_id"] for i in back] == ["c1", "c2"]
    assert [i["name"] for i in back] == ["read_file", "search_code"]
    assert json.loads(back[1]["arguments"]) == {"pattern": "x"}


# -- the canonical stub the rest of the graph consumes ---------------------


def test_the_translated_response_drives_the_existing_router():
    """Proof the boundary holds: Phase 1 routing is untouched by provider."""
    from agent.model_client import _Response
    from agent.nodes import after_agent

    resp = _Response(_openai_canonical(
        _resp([_call("submit_findings", '{"summary":"s","findings":[]}')])
    ))
    state = {"status": "running", "messages": [
        {"role": "assistant",
         "content": [b.model_dump(exclude_none=True) for b in resp.content]}]}

    assert after_agent(state) == "execute_tools"


def test_a_translated_response_serialises_into_a_cassette():
    """Cassette format is provider-neutral, so recordings keep replaying."""
    from agent.model_client import _Response, _serialise

    resp = _Response(_openai_canonical(
        _resp([_call("read_file", '{"path":"a.py"}')], _usage(10, 2, reasoning=4))
    ))
    d = _serialise(resp)

    assert d["content"][0]["name"] == "read_file"
    assert d["usage"]["reasoning_tokens"] == 4
    assert _Response(d).content[0].input == {"path": "a.py"}


# -- cassette namespacing --------------------------------------------------


def test_cassettes_are_namespaced_by_provider_and_model():
    from evals.runner import cassette_name

    settings = _Obj(provider="openai", model="gpt-5.6-terra")
    assert cassette_name({"id": "wide-refactor"}, settings) == (
        "wide-refactor.openai.gpt-5.6-terra"
    )


def test_the_two_providers_cannot_collide_on_one_cassette():
    from evals.runner import cassette_name

    a = cassette_name({"id": "x"}, _Obj(provider="openai", model="gpt-5.6-terra"))
    b = cassette_name({"id": "x"}, _Obj(provider="anthropic", model="claude-haiku-4-5"))
    assert a != b


def test_an_explicit_cassette_name_still_wins():
    from evals.runner import cassette_name

    settings = _Obj(provider="openai", model="gpt-5.6-terra")
    assert cassette_name({"id": "x", "cassette": "pinned"}, settings) == "pinned"


# -- config ----------------------------------------------------------------


def test_the_output_allowance_is_large_enough_for_a_reasoning_model():
    """4096 was the Anthropic ceiling. A reasoning model can spend all of it
    thinking and return nothing visible, which would degrade every run."""
    from config import get_settings

    s = get_settings()
    assert s.max_output_tokens >= 16_000
    assert s.reasoning_effort in {"none", "low", "medium", "high", "xhigh", "max"}
