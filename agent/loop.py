import json
import os

from anthropic import Anthropic

from .models import ReviewFindings
from .state import State
from .tools import DISPATCH, TOOL_SCHEMAS

MAX_ITERATIONS = 10


def _extract_json(text: str) -> str:
    return text[text.find("{") : text.rfind("}") + 1]

SYSTEM = """You are a PR reviewer. Investigate the diff using the tools provided.
The repository you can read with tools is the code AFTER this diff is applied.
Cite added code from the diff or from the files; both agree.
When you are done, output ONLY a JSON object matching this schema (no markdown fences, no other text):
"""


def system_prompt() -> str:
    return SYSTEM + json.dumps(ReviewFindings.model_json_schema())


def run(diff: str, repo_root: str, trace: list | None = None) -> ReviewFindings:
    client = Anthropic()
    state = State()
    state.add_user(f"Review this PR diff:\n\n{diff}")
    system = system_prompt()
    model = os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MODEL", "claude-sonnet-4-20250514")
    max_tokens = int(os.environ.get("MAX_TOKENS", "4096"))

    for i in range(1, MAX_ITERATIONS + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=TOOL_SCHEMAS,
            messages=state.messages,
        )

        print(f"--- {i}/{MAX_ITERATIONS} ---")
        print(
            f"tokens est={state.token_count()}  "
            f"api in={resp.usage.input_tokens} out={resp.usage.output_tokens}"
        )
        step = {
            "iteration": i,
            "tokens_est": state.token_count(),
            "api_in": resp.usage.input_tokens,
            "api_out": resp.usage.output_tokens,
            "stop_reason": resp.stop_reason,
        }

        if resp.stop_reason == "tool_use":
            state.add_assistant(resp.content)
            results = []
            calls = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                print(f"tool {block.name} {block.input}")
                try:
                    fn = DISPATCH[block.name]
                    result = fn(repo_root=repo_root, **block.input)
                except Exception as e:
                    result = f"error: {type(e).__name__}: {e}"
                print(f"preview: {result[:200]}")
                results.append((block.id, result))
                calls.append({"name": block.name, "input": dict(block.input), "result": result})
            state.add_tool_results(results)
            step["tools"] = calls
            if trace is not None:
                trace.append(step)
            continue

        text = "".join(block.text for block in resp.content if block.type == "text")
        print(f"preview: {text[:200]}")
        step["text"] = text
        if trace is not None:
            trace.append(step)
        return ReviewFindings.model_validate_json(_extract_json(text))

    raise RuntimeError("hard stop at 10 iterations")



