# Auto PR

## What this is
A PR review agent. Current phase: local prototype, one repo on disk.

## Hard constraints
- No frameworks (LangGraph, LlamaIndex). Raw loop only.
- No embeddings/vector DB. Keyword + AST search first.
- No queue, no DB, no Docker, no async until explicitly asked.
- Python, Pydantic for all model output.

## Working style
- Explain approach and tradeoffs BEFORE writing code. Wait for my ack.
- Small diffs. One concern per change.
- If I'm overengineering, say so.

## Do not touch
agent/state.py — I write this by hand.

Slice 1 may edit `loop.py` (post-change prompt, tool errors as tool_result)
and `tools.py` (ignore list, output caps, catch-and-return). Nothing else.