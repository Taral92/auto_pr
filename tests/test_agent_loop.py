from agent.nodes import after_agent


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
