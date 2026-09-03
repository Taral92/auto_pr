# Open work

## Done

Cycle A is closed. A real GitHub webhook (`delivery_id 04765dd0-a7a3-11f1-...`)
drove a run that posted inline comments on PR #3 as `pr-review-10x[bot]`.

Postgres verified with two workers and lease recovery. Hermetic evals:

```
no-defect        0 findings emitted        (stayed silent on correct code)
sandbox-escape   tp 2  fp 0  precision 1.0  recall 1.0  inline_rate 1.0
```

## What production says that the eval cannot

Three runs on PR #3, all `budget_breach:iterations`:

```
143k in   5 grounded   4 inline
 89k in   1 grounded   1 inline
106k in   3 grounded   2 inline
```

Not three judgments - three cut-off points. The agent never reaches
`end_turn` on a real PR, so every review is work-in-progress, and that is
the whole of the 5/1/3 spread.

The fixture is 6 files with 1 changed. It finishes in 2-3 iterations and
exits cleanly, so the eval cannot reproduce the only failure production has.

## 1. Tell the reader the review is partial  ← START HERE

`degrade()` sets `status` and `error` in state, and nothing reaches the
review body. A human sees a confident review with no sign the agent stopped
mid-investigation. The design rule is "publish what exists WITH THE REASON
ATTACHED"; the reason is not attached.

- [ ] `build_review` takes the breach reason and prefixes the body:
      "Review stopped early (iteration budget). Findings below are partial."
- [ ] test: a degraded review body says so; a clean one does not

## 2. Make degradation visible to the eval

- [ ] runner reports `state` and `iterations` per case
- [ ] a case that degrades is not scored as a clean pass

## 3. A fixture big enough to fail

- [ ] `evals/fixtures/wide-refactor/` - 25+ files, 4-6 changed, 2 planted
      defects. Must reproduce `budget_breach:iterations` on today's code.
- [ ] record it. Baseline: iterations, tokens, findings, variance over
      `--live --reps 5`

Without this, nothing below is measurable.

## 4. Blast radius - the actual fix

The agent spends its budget working out what to look at. Give it that up
front instead.

- [ ] parse the diff to changed files + changed symbols
- [ ] per symbol, exact lookup: references, importers, tests naming it
- [ ] put the ranked list in the first message; expose `find_references`,
      `find_tests` as tools
- [ ] grep + AST. No embeddings.
- [ ] measure against the task 3 baseline: iterations down, `published`
      instead of `degraded`, variance down

## 5. Close cycle B - the feedback loop

Nothing reads what happens to a posted comment, so `FIND_COST_MIN` stays a
guess and "net +24 min/PR" is an assumption, not a measurement.

- [ ] webhook on `pull_request_review_comment` and thread resolution
- [ ] label each finding: dismissed / resolved-without-change / edited-then-committed
- [ ] feed labels back into the eval corpus
- [ ] replace the constants with observed data

## Still unverified

- [ ] push twice fast -> older run superseded, one review
- [ ] redeliver from GitHub's UI -> no duplicate comment
      (dedupe landed in 5e1e27f; confirm the PR #3 runs postdate it)
