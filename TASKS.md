# Open work

## Cycle A is closed

A real GitHub webhook drove a run end to end and posted inline comments:

```
POST /webhook 202  ->  queued  ->  worker  ->  clone  ->  review
                   ->  ground  ->  anchor  ->  comments on the PR
```

Postgres verified with two workers and lease recovery. Hermetic evals:

```
no-defect        0 findings emitted     (silent on correct code)
sandbox-escape   tp 2  fp 0  precision 1.0  recall 1.0  inline_rate 1.0
```

Setup notes worth keeping: smee.io times out under GitHub's delivery
deadline - use ngrok. The webhook URL must include the `/webhook` path;
without it every delivery 404s at the root and looks like a filtered event.

## THE FINDING: iteration exhaustion is a SCOPE problem, not a size problem

Ten iterations on a 2-file PR, from the live trace:

```
it1  list_files evals/fixtures                    -> path not found
it2  search_code 'def _resolve.*escape.*symlink'  -> empty
it3  search_code '_resolve.*resolve.*parents'     -> empty
it4  search_code 'root in target\.parents'        -> 1 hit
it5  search_code 'blank.*line.*end|trailing'      -> empty
it6  search_code 'is_cancelled.*lambda'           -> 1 hit
it7  search_code 'ON CONFLICT|SKIP LOCKED'        -> hits
it8  search_code 'hmac\.compare_digest'           -> 1 hit
it9  search_code 'SecretStr'                      -> hits
it10 search_code 'copytree|mkdtemp'               -> hits
```

- Not one of those symbols is in the diff. It audited the repo against the
  design rules in our own docs instead of reviewing the change.
- Four of ten searches returned nothing - speculative regexes guessing at
  code that might exist. Each costs a whole iteration.
- **It never called read_file on a changed file.** Ten iterations, zero
  looks at what the PR actually did.

Earlier runs degraded on a 60-file PR and we blamed repo size. Wrong: it
degrades on 2 files too. The prompt says stay in scope and the tools let it
leave, so searching feels productive and the budget goes.

## 1. Enforce scope in the tools, not just the prompt — DONE

- [x] `core.diff.changed_paths` parses the diff once; `assemble_context`
      puts it in state as `scope`
- [x] `read_file` outside that set returns the refusal, naming what IS
      readable; `search_code` walks only the changed files
- [x] `list_files` removed - scoped it echoes the manifest, unscoped it is
      the hole. It was iteration 1 of the failing run.
- [x] escape hatch deferred to task 3 on purpose, not the general search
- [x] tests: refusal not content, deleted files, path canonicalisation,
      escape-beats-scope ordering, and the diff -> state -> tool wiring

## 2. Make an empty result cost something — DONE

Four wasted iterations returned "" and read like normal answers.

- [x] `search_code` always reports what it searched, so 0 hits cannot read
      as an answer; `is_unproductive` classifies error/refusal/no-hit
- [x] `execute_tools` appends "[N iterations left]" to those results only -
      never to the corpus, so a budget note can never become evidence
- [x] test

Not yet measured: today's fixtures exit in 2-3 iterations, so neither 1 nor
2 can be scored until task 4 exists. Green tests are not evidence here.

## 3. Blast radius

Give it the dependency map up front so it does not have to search for one.

- [ ] diff -> changed files + changed symbols
- [ ] per symbol: references, importers, tests naming it (grep + AST)
- [ ] ranked list in the first message; `find_references` / `find_tests`
      as explicit tools
- [ ] measure against a baseline: iterations down, `published` not
      `degraded`, variance down

## 4. A fixture that reproduces the failure

The hermetic fixture is 6 files and exits in 2-3 iterations, so the eval
cannot see any of the above.

- [ ] `evals/fixtures/wide-refactor/` - 25+ files, 4-6 changed, 2 planted
      defects, must reproduce `budget_breach:iterations` on today's code
- [ ] runner reports `state` and `iterations`; a degraded run is not a pass

## 5. Close cycle B - the feedback loop

Nothing reads what happens to a posted comment, so FIND_COST_MIN is a guess
and "net +24 min/PR" is an assumption, not a measurement.

- [ ] webhook on `pull_request_review_comment` + thread resolution
- [ ] label findings: dismissed / resolved-without-change / edited-then-committed
- [ ] feed labels into the eval corpus, replace the constants with data

## Still unverified

- [ ] push twice fast -> older run superseded, one review
- [ ] redeliver -> no duplicate comment
- [ ] `gh/ratelimit.py` ground truth (3 planted defects) never scored
