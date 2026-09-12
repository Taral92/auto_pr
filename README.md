# auto-pr

A pull request review agent, packaged as a hosted GitHub App. A PR opens, the
agent reads the diff, investigates the checked-out code with tools, and posts
inline review comments — but only for findings whose evidence it can prove it
actually saw.

## How it runs

```
GitHub ──webhook──► api (verify HMAC, coalesce, INSERT, 202)
                          │
                     postgres: runs
                          │
                          ▼
                     worker (FOR UPDATE SKIP LOCKED)
                          │
        installation token ─┤ clone head SHA
                            │
                       LangGraph: assemble → agent_step ⇄ execute_tools
                            │                → submit_findings → ground
                            ▼
                       anchor → POST review ──► GitHub
```

The webhook never calls the model. It signs off in under a second; the worker
does the minutes-long work. That split is the whole reason a queue exists here.

## Model and provider

The agent talks to **OpenAI's Responses API**, on **`gpt-5.6-luna`** with
`REASONING_EFFORT=low`. Luna was picked by measurement: on the `wide-refactor`
fixture it matched the larger `gpt-5.6-terra` on precision, recall,
groundedness and anchor rate, at about a seventeenth of the cost.

Both candidates are reasoning models, which is why `MAX_OUTPUT_TOKENS` (32,000)
dwarfs the `MAX_TOKENS` ceiling the Anthropic path uses. Reasoning tokens are
billed as output *and* counted against that limit, and can exhaust it before a
single visible token appears — which would starve the `submit_findings` call.

`PROVIDER=anthropic` switches to Claude instead. The provider boundary is
`agent/model_client.py`: it translates either API into one canonical message
format, so the graph, budgets, grounding, scope jail and change map are
provider-agnostic. Both paths stay live so the same fixtures can be scored
against either.

Prompt caching needs no configuration on the OpenAI path — it is implicit above
1,024 tokens and comes back as `cached_tokens` / `cache_write_tokens`. Those
count toward the token budget: a cached token is cheaper, but it still occupies
the context window and is re-sent on every turn.

## Local

```bash
docker compose up -d db
cp .env.example .env          # OPENAI_API_KEY and GITHUB_TOKEN at minimum
pip install -r requirements.txt

uvicorn api.main:app --port 8000
python -m worker.main

# expose the webhook to GitHub during development
npx smee-client --url https://smee.io/<channel> --target http://localhost:8000/webhook
```

Or the whole stack: `docker compose up --build`

## GitHub App setup

1. Create an App. Permissions: **Contents: Read**, **Pull requests: Read & write**.
   Events: **Pull request**.
2. Webhook URL → your `/webhook`. Set a webhook secret.
3. Download the private key, then:
   `base64 -i key.pem | tr -d '\n'` → `GITHUB_APP_PRIVATE_KEY`
4. Install it on a repo. Open a PR.

## CLI

```bash
python -m agent.cli review https://github.com/owner/repo/pull/1 --dry-run
```

Uses a PAT (`GITHUB_TOKEN`). Same code path as the worker — `review_pr()`
takes either a token string or a token provider.

## Layout

```
core/     domain models + error taxonomy (no I/O)
agent/    LangGraph pipeline, tools, prompt, grounding, budget, change map,
          provider translation, record/replay
gh/       GitHub API, App auth, webhook verification, clone, anchoring
storage/  postgres: runs + findings
api/      FastAPI — webhook + operator endpoints. Never runs a review.
worker/   claims a job, runs it, records the result
evals/    hermetic fixtures, scoring, per-provider/model cassettes
```

## The parts that matter

**Grounding gate.** Every finding's `evidence` must be an exact substring of
what the agent actually saw — the diff plus every tool result. Paraphrases are
dropped. This decides whether the agent is worth leaving switched on.

**Anchoring.** GitHub rejects a comment on a line outside the diff, so line
numbers are computed from `@@` hunk headers, never taken from the model.
Grounded and in the diff → inline comment. Grounded but elsewhere → summary.
Otherwise dropped.

**Coalescing.** On a new push, queued runs for that PR are superseded and
running ones cancelled. Cost scales with pull requests, not with pushes.

**Idempotency.** A hidden marker keyed on `(repo, pr, head_sha)` goes in the
review body. Redelivery finds it and skips. `prompt_sha` is deliberately *not*
in the key — editing the prompt would otherwise re-review every open PR. A new
review comes from a new commit, which `head_sha` already covers.

**Leases.** `LEASE_S` must exceed `MAX_WALL_CLOCK_S` — the worker refuses to
start otherwise — and a heartbeat extends it while a job runs. Too short and a
slow review is reclaimed and posted twice; too long and a crashed worker's job
sits stuck.

**Completion.** The agent finishes by calling `submit_findings`, whose schema
the API validates. Completion used to be inferred from the absence of a tool
call, which made "finished" and "wandered off protocol" the same event and
left a brace-matching parser to tell them apart.

**Budgets.** Tokens, wall clock and cumulative tool bytes are the work budget —
what a review costs. Turns and consecutive dead ends are fuses: they catch a
loop going nowhere, which costs nothing and so is invisible to the rest. A
breach degrades: one constrained `submit_findings` call publishes what exists
with the reason attached, never go silent.

**Bounded reads.** A file under the read cap comes back whole. Over it,
`read_file` returns windows around the diff's own hunks — 80 lines either side,
merged where they overlap, each gap replaced by a marker naming the omitted line
range. Head-truncating from byte zero once returned 1,609 lines of a generated
lookup table and cut off the only code the diff changed. The byte cap remains as
a backstop for a single enormous hunk, and a changed file with no hunks falls
back to truncation.

**Change map.** Before the first model turn, a deterministic pass over the
checkout works out what the diff touches: the changed symbols (AST definitions
intersected with the hunk ranges) and, for files that import a changed module,
where those symbols are defined, used, or named by a test. It is sorted and
bounded, so one diff always yields one map, and it rides in the first user
message inside the cached prefix. Python only today; other languages degrade to
an empty map. It *reports* paths outside the diff without making them readable,
so the scope jail is unchanged — and it is triage information, never evidence:
the map lives in the message and never in the corpus, so nothing quoted from it
can ground a finding.

**Sandbox.** Tools are jailed to the checkout — `../`, absolute paths and
escaping symlinks refused. Tool errors return as `tool_result` content, so a
bad regex costs one iteration rather than the run. Model-chosen tool calls run
over untrusted repository content; the container runs non-root.

## Evals

```bash
python -m evals.runner              # replay — free, offline, deterministic
python -m evals.runner --record     # calls the API, saves cassettes
python -m evals.runner --reps 5     # variance
```

Cassettes are namespaced `<case>.<provider>.<model>`, because a recording is a
record of how one model behaved — replaying one model's transcript while
configured for another would score the wrong thing.

Two questions, measured separately:

- **Correct?** precision, recall, groundedness, anchor rate
- **Worth it?** `net = Σ find_cost(true positives) − false_positives × 1.5min`

Only findings that reach a human count as false positives — one the gate
dropped costs nobody anything. Missed defects score zero, not negative;
unaided, the developer misses them too. Below the printed break-even
precision, the agent costs more time than it saves.

Fixtures are **hermetic**: the reviewed tree contains no eval data. An earlier
live-PR eval read its own answer key out of `evals/cases/`, which made every
number from it meaningless.
