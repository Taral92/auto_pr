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
                            │                → parse → ground
                            ▼
                       anchor → POST review ──► GitHub
```

The webhook never calls the model. It signs off in under a second; the worker
does the minutes-long work. That split is the whole reason a queue exists here.

## Local

```bash
docker compose up -d db
cp .env.example .env          # fill it in
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
agent/    LangGraph pipeline, tools, prompt, grounding, record/replay
gh/       GitHub API, App auth, webhook verification, clone, anchoring
storage/  postgres: runs + findings
api/      FastAPI — webhook + operator endpoints. Never runs a review.
worker/   claims a job, runs it, records the result
evals/    hermetic fixtures, scoring, cassettes
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

**Idempotency.** A hidden marker keyed on `(repo, pr, head_sha, prompt_sha)`
goes in the review body. Redelivery finds it and skips. A prompt change is
legitimately a new review.

**Leases.** `LEASE_S` must exceed `MAX_WALL_CLOCK_S` — the worker refuses to
start otherwise — and a heartbeat extends it while a job runs. Too short and a
slow review is reclaimed and posted twice; too long and a crashed worker's job
sits stuck.

**Budgets.** Iterations, tokens, wall clock, cumulative tool bytes. A breach
degrades: publish what exists with the reason attached, never go silent.

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
