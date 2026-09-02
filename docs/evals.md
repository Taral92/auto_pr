# Evals

Two questions, deliberately separated:

1. **Is the agent correct?** precision / recall / groundedness / anchor rate
2. **Does it save developer time?** a different question with a different answer

## Run it

```
python -m evals.runner                    # all cases, replay, free
python -m evals.runner --reps 5           # variance across runs
python -m evals.runner --case pr-001
python -m evals.runner --record           # spend tokens, save cassettes
python -m evals.runner --json             # machine-readable
```

Replay is the default. An eval you cannot afford to run is an eval you will
not run.

## Record / replay

`agent/model_client.py`. Three modes via `MODEL_MODE`:

| mode | behaviour |
|---|---|
| `live` | call the API (default for normal runs) |
| `record` | call the API and write `evals/cassettes/<name>.json` |
| `replay` | read responses off disk, never touch the network |

Cassettes are keyed by **call order**, not request hash. Hashing looks tidier
but invalidates the cassette the moment you change the prompt - which is
exactly when you most want the old one to still replay.

Record once. Then every change to grounding, anchoring, budgets, parse repair
and the degrade path is testable at zero cost and with zero variance.

## Cases

`evals/cases/*.json`. Ground truth is anchored to code, not to titles:

```json
{
  "id": "jail-removed",
  "path": "agent/tools.py",
  "category": "security",
  "severity": "blocker",
  "must_contain": ["read_bytes()"]
}
```

Matching on titles is hopeless - the model rewords them every run. Matching on
`(path, category, substring)` is stable across runs and prompt versions.

**`clean.json` is a negative control**: a diff with no defect, where any
published finding is a false positive. Without it you cannot tell a careful
reviewer from a quiet one.

## What counts as a false positive

Only findings that would **reach a human**: `anchored` in (`inline`, `summary`).

A finding the grounding gate dropped never reaches the PR and costs nobody any
time. Counting dropped findings as false positives would punish the gate for
doing its job.

## The time model

```
saved  = sum(find_cost of each true positive)
wasted = false_positives x dismiss_cost
net    = saved - wasted
```

| constant | value | meaning |
|---|---|---|
| `FIND_COST_MIN[blocker]` | 12 min | a human finding this unaided |
| `FIND_COST_MIN[should_fix]` | 5 min | |
| `FIND_COST_MIN[nit]` | 1 min | |
| `DISMISS_COST_MIN` | 1.5 min | reading a wrong comment and dismissing it |

Deliberately conservative. Inflating `FIND_COST_MIN` makes any agent look good.

**Missed defects score 0, not negative.** Without the agent the developer was
going to miss them anyway. An agent is not made worse by failing to help.

### The consequence

Every false positive is charged to a human's attention. Which means:

> **A noisy agent costs time even when it is sometimes right.**

Two real findings buried in twenty wrong ones is **-6 minutes per PR**, and a
team switches it off inside a week.

`breakeven_precision()` prints the precision below which the agent is a net
loss. For two blockers at 12 min against a 1.5 min dismissal cost that is
**11%** - low, because blockers are expensive to find by hand. Raise
`DISMISS_COST_MIN`, or review cheaper defects, and the bar climbs fast.

**This is why precision matters more than recall here**, and why the grounding
gate is the highest-leverage component in the system: it converts would-be
false positives into silence, which is free.

## Honest limits

- `FIND_COST_MIN` is a guess, not a measurement. It is a consistent yardstick
  for comparing prompt versions to each other - not a claim about real hours.
- To make it a real claim you need humans: ship it, log which comments get
  resolved vs dismissed, and replace the constants with observed data.
  That is the feedback loop in `docs/pipeline.md`, and it cannot be faked
  offline.
- Two cases is not a benchmark. Add a case every time a real PR surfaces a
  failure the suite did not catch.
