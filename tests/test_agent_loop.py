from agent.nodes import _call_sig, _prior_tool_sigs, after_agent


def test_tool_stop_without_tool_block_does_not_create_empty_result_turn():
    state = {
        "status": "running",
        "stop_reason": "tool_use",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": '{"findings":[]}'}],
            }
        ],
    }

    assert after_agent(state) == "parse_findings"


def test_tool_block_routes_to_tool_execution():
    state = {
        "status": "running",
        "stop_reason": "tool_use",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-1",
                        "name": "read_file",
                        "input": {"path": "app/files.py"},
                    }
                ],
            }
        ],
    }

    assert after_agent(state) == "execute_tools"


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
