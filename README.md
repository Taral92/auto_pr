# auto-pr

A pull request review agent. Reads a PR diff, investigates the checked-out
code with tools, and posts inline review comments — but only for findings
whose evidence it can prove it actually saw.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

`.env`:

| key | notes |
|---|---|
| `ANTHROPIC_API_KEY` | |
| `GITHUB_TOKEN` | fine-grained: Contents R/W, Pull requests R/W |
| `MODEL` | default `claude-haiku-4-5-20251001` |

## Run

```bash
# review one PR
python -m agent.cli review https://github.com/owner/repo/pull/1 --dry-run

# service: API enqueues, worker executes
uvicorn api.main:app --port 8000
python -m worker.main

# tests
pytest -q

# evals
python -m evals.runner                    # replay, free
python -m evals.runner --record           # calls the API, saves cassettes
python -m evals.runner --reps 5           # variance
```

## Layout

```
core/     domain models + error taxonomy (no I/O)
agent/    LangGraph pipeline, tools, prompt, grounding
gh/       GitHub API, clone, diff parsing, anchoring
storage/  sqlite: runs + findings
api/      FastAPI — enqueues only, never executes a review
worker/   claims a job, runs it, records the result
evals/    cases, scoring, cassettes
```

## How a review works

```
fetch PR + diff → clone head SHA → LangGraph
  assemble_context → agent_step ⇄ execute_tools → parse → ground
→ anchor → publish → delete workspace
```

**Grounding gate.** Every finding's `evidence` must be an exact substring of
what the agent actually saw — the diff plus every tool result. Paraphrases are
dropped. This is the component that decides whether the agent is worth leaving
switched on.

**Anchoring.** GitHub rejects a comment on a line outside the diff, so line
numbers are computed from `@@` hunk headers rather than taken from the model.
Grounded and in the diff → inline comment. Grounded but not in the diff →
summary. Otherwise dropped.

**Budgets.** Iterations, tokens, wall clock, and cumulative tool bytes. A
breach degrades: it publishes what it has with the reason attached, rather
than going silent.

**Sandbox.** Tools are jailed to the checkout — `../`, absolute paths and
escaping symlinks are refused. Tool errors return as `tool_result` content so
one bad regex costs an iteration, not the run.

## Evals

Two questions, measured separately:

- **Correct?** precision, recall, groundedness, anchor rate
- **Worth it?** `net = Σ find_cost(true positives) − false_positives × 1.5min`

Only findings that reach a human count as false positives — a dropped finding
costs nobody anything. Missed defects score zero, not negative; unaided, the
developer misses them too.

Below the break-even precision the agent costs more time than it saves. The
runner prints that threshold every time.

Record once with `--record`, then iterate on replay for free.
