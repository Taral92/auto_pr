# Auto PR

## What this is
A PR review agent. LangGraph is the agent loop (`agent_step` + `execute_tools`).
FastAPI enqueues; a separate worker calls `review_pr()`.

## Hard constraints
- No `create_react_agent`, `ChatAnthropic`, or any `langchain` / `langchain_*` import.
- The agent does not import GitHub. `gh/` owns fetch, clone, anchor, publish.
- `review_pr()` signature is unchanged. The API never runs a review.
- Python, Pydantic for all model output.

## Working style
- Explain approach and tradeoffs BEFORE writing code. Wait for my ack.
- Small diffs. One concern per change.
- If I'm overengineering, say so.
