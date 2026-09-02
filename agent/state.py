import json


class State:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, content) -> None:
        self.messages.append({
            "role": "assistant",
            "content": [block.model_dump(exclude_none=True) for block in content],
        })

    def add_tool_results(self, results: list[tuple[str, str]]) -> None:
        self.messages.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
                for tool_use_id, content in results
            ],
        })

    def token_count(self) -> int:
        return len(json.dumps(self.messages)) // 4

