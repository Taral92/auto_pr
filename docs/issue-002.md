# Issue #2 — Make findings machine-checkable

## Why

Run `2026-08-30T21-15-09` found both real bugs in `pr-001.diff` and then produced
output that cannot be verified by a machine:

- All three `evidence` strings are paraphrases. None is a substring of any file
  or any tool result. A string-match verifier fails on a *true* finding.
- 2 of 3 `line` values are wrong. The model did diff-header arithmetic and missed.
- `severity` / `category` are free-form strings, so no two runs are comparable.
- The one `read_file` call contributed nothing: the repo on disk is the pre-image,
  and every citation came from the diff already in the prompt. Nothing told the
  agent which side of the change it was holding.

This issue does not try to make the agent smarter. It makes its output falsifiable.

## Scope

Four changes. Touches `agent/models.py`, `agent/loop.py`, `scripts/run.py`, and
adds `agent/grounding.py`.

Explicitly NOT in this issue:
- No git checkout, no merge-base/head trees. (Deferred until we can measure
  whether the agent wants to read outside the diff at all — right now it doesn't.)
- No repair loop on a failed finding. Count first, repair later.
- No `expected.json`, no scorer. That is issue #3 and depends on this one.
- No change to the loop's control flow, retries, or error handling.

---

## Change 1 — Tell the agent which side of the diff the repo is

Add to `SYSTEM` in `agent/loop.py`, verbatim intent:

> The repository at the tool root is the code as it exists BEFORE this diff is
> applied. The diff describes proposed changes that are NOT present in any file
> you read. When you cite code that the diff adds, quote it from the diff. When
> you cite existing code the diff does not change, quote it from a file you read.

Cheapest fix in the project. It is the direct cause of the pre-image confusion.

## Change 2 — Schema

Two models now, not one. What the model emits is not what gets published.

**`Finding`** — what the model emits:
- `severity`: enum `blocker | should_fix | nit`
- `category`: enum `correctness | security | performance | maintainability | test_gap`
- `file`: str
- `title`, `description`, `recommendation`: str
- `evidence`: str — **a verbatim span, copied exactly from the diff or from a
  tool result.** Not a summary. Not reformatted. Not joined with semicolons.
- **Remove `line`.** The model is bad at it and does not need to be good at it —
  a verbatim span locates itself.
- **Remove `confidence`.** It came back 0.99 / 0.99 / 0.7 and nothing consumes it.
  Reintroduce it when something gates on it.

**`GroundedFinding`** — what the gate produces:
- everything from `Finding`, plus
- `verdict`: enum `grounded | near | ungrounded`
- `source`: str — where the evidence matched (`"diff"` or `"read_file:agent/tools.py"`)
- `line`: int | None — computed by us, from the match position

Three severities, not five. Fewer levels means less model indecision and better
agreement between runs — which is the whole point of this issue.

## Change 3 — The grounding gate (`agent/grounding.py`)

Runs after the loop returns, before anything is printed or published.

**Corpus** = the initial diff string + the `content` of every tool result in the
run. That is the agent's complete observable universe. Keep each corpus entry
labelled with its source so `source` can be filled in.

**Verdict per finding:**
- `grounded` — `evidence` is an exact substring of some corpus entry
- `near` — no exact match, but matches after collapsing whitespace runs and
  normalising line endings
- `ungrounded` — no match either way

**Action:** keep `grounded`, drop the rest. Record counts of all three.

The `near` bucket is not politeness — it is the measurement. `near` means the
model is reflowing text; `ungrounded` means it is inventing or summarising.
Those are different problems with different fixes, and collapsing them into one
"failed" number throws away the signal.

**Known hole:** a one-word `evidence` like `os.system` passes trivially. Enforce a
minimum span — at least one full line, or ~20 characters. Do not build anything
cleverer yet; log how often findings sit near the minimum and decide from data.

## Change 4 — Trace

Add to the run payload in `scripts/run.py`:
- `grounding`: `{grounded: n, near: n, ungrounded: n}`
- `corpus_entries`: count, and total chars
- `prompt_sha`: hash of the system prompt + schema actually sent
- `wall_clock_s`, `tokens_in_total`, `tokens_out_total`

Without `prompt_sha` you cannot tell later which prompt produced which run, and
every comparison you make after this point is unsound.

---

## Acceptance

Run against `fixtures/pr-001.diff` and check:

1. Every finding carries a verdict and a `source`.
2. Findings that survive the gate have a `line` we computed, not one the model guessed.
3. `severity` and `category` are enum members, so two runs are comparable.

**Prediction to test against:** on the previous run's output, all three findings
would have failed exact match. If the first run after this change comes back
3/3 grounded, be suspicious — check whether the spans are trivially short before
believing the prompt change worked that well.

## Before merging, explain back

- Why is the corpus the run's *observations* rather than the repo on disk?
- What does `near` tell you that `ungrounded` does not, and what would you do
  differently depending on which one dominates?
- What is the cheapest way for the model to game this gate, and how would you
  detect it in the trace?

## Note

`agent/claude.md` says *"Do not touch agent/loop.py, agent/state.py,
agent/tools.py — I write these by hand."* This issue edits `loop.py`. Either
update that line or hand-write Change 1 yourself; Cursor will otherwise either
refuse or ignore its own instructions, and both are worse than deciding now.
