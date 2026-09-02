# auto-pr as a hosted GitHub App — build order

## Decision

v1 is a hosted GitHub App: installable on an org, reacts to PRs unattended,
posts reviews. This retires an earlier scope constraint — see "Retired" below.

## The ordering rule

**Milestone A must post a real review comment before a single line of webhook
code exists.**

The App is an *ingress* wrapped around a core that has to work first. Webhook
registration, JWT signing, tunnels and secret handling are 0% agent work. Built
first, they cost days and produce nothing visible. Built second, they wrap
something that already works and each piece is testable on its own.

---

## Milestone A — the core review path

Entry point: `python -m agent.cli review <pr-url>`, auth via a personal token in
`GITHUB_TOKEN`. Run by hand. No server, no App, no queue.

1. **Resolve** the URL to `owner/repo/number`. Fetch the PR: you need
   `head.sha` and `base.sha`.
2. **Diff**: `GET /repos/{o}/{r}/pulls/{n}` with `Accept: application/vnd.github.v3.diff`.
   This replaces `fixtures/pr-001.diff` as the input.
3. **Checkout** the head SHA into a temp dir:
   `git init` → `git remote add origin <https url with token>` →
   `git fetch --depth=1 origin <head_sha>` → `git checkout FETCH_HEAD`.
   This is the first time `repo_root` is unambiguously the post-change tree —
   which retires the pre/post confusion from run `2026-08-30T21-15-09`
   for free. Update the system prompt to say so.
4. **Run** the existing loop, unchanged.
5. **Gate** — see below. Not optional here, for API reasons.
6. **Publish**: `POST /repos/{o}/{r}/pulls/{n}/reviews` with
   `{event, body, comments: [{path, line, side, body}]}`.

### Why the grounding gate is now load-bearing

**GitHub rejects an inline comment anchored to a line that is not part of the
diff.** The API returns 422. So the line number you attach to a finding is not
a nicety for eval — it is the difference between a review that posts and a
request that fails.

The model cannot be trusted with it (2 of 3 wrong in the last run). So:

- The model emits a **verbatim `evidence` span**, not a line number.
- You locate that span in the diff and compute the line and `side` yourself.
- Span found in the diff → inline comment.
- Span found only in a file the agent read, not in the diff → **not droppable,
  but not anchorable either**: it goes in the review body as a summary point.
- Span found nowhere → drop it and count it. That one was invented.

Three outcomes, and the API forces the distinction. This is the same gate as
`docs/issue-002.md`, arrived at from the product side.

### First real test subject

Open a PR on `auto-pr` itself that applies `fixtures/pr-001.diff`. Real API,
real webhook later, and ground truth you already know.

---

## Milestone B — the App shell

Only once A posts comments.

1. **Register** the App. Permissions: `pull_requests: write`,
   `contents: read`, `metadata: read`. Events: `pull_request`
   (`opened`, `synchronize`, `reopened`). Keep it minimal — orgs read this
   list at install time and a broad ask gets declined.
2. **Auth chain** — this is genuinely different from the CLI and is where the
   time goes:
   App private key → RS256 JWT (max 10 min) → installation access token
   (**expires in 1 hour**). A long review can outlive its token. Refresh on
   use, not once at startup.
3. **Webhook endpoint**: verify `X-Hub-Signature-256` HMAC, return `202`
   immediately, enqueue. It must never call the model. GitHub retries anything
   slower than ~10s.
4. **Job store**: a SQLite table — `id, delivery_id, repo, pr, head_sha, state,
   attempts, created_at, leased_until`. Not Redis, not Celery, not Docker.
5. **Worker**: a second process that polls, claims a row with a conditional
   update, and calls Milestone A's function. Two processes, one SQLite file,
   one VM.
6. **Local dev**: webhooks cannot reach localhost. Set up a smee.io channel on
   day one — this is the single most common place to lose an afternoon.

## Milestone C — production ingress

1. **Coalescing**: on a new `synchronize` for `(repo, pr)`, cancel any queued or
   in-flight job with an older `head_sha`. Without this your cost scales with
   pushes, not PRs.
2. **Idempotency**: GitHub *will* redeliver. Key on
   `(repo, pr, head_sha, prompt_sha)` and skip if already published, or every
   redelivery double-posts every comment.
3. **Budgets**: wall-clock and cost ceilings, and a degraded path that publishes
   partial findings with the reason attached rather than going silent.
4. **Dead-letter with a surface** you actually look at. A review agent that
   silently covers 80% of PRs while everyone assumes 100% is worse than none.

---

## Traps specific to the App (not present in a CLI)

- Installation tokens expire mid-run. Design for refresh.
- The App private key is the entire security boundary. Never in the repo, never
  in a committed `.env`. This is the one place where "learning project, no error
  handling" is not an acceptable posture.
- `pull_request.synchronize` fires on **every push**, including force-pushes and
  rebases. This is what makes Milestone C.1 mandatory rather than nice.
- Disk: one clone per job on an ephemeral worker fills up fast. Clean up in a
  `finally`, and bound total workspace size.
- A PR can be huge. Cap diff size and bail with an explicit "too large to
  review" comment rather than blowing the context window silently.

---

## Retired

**"No queue, no DB"** is now void — the ack-fast / work-slow split requires
somewhere to put the work. The minimum honest version is a SQLite job table
and a polling worker. Deliberately *not* Redis, Celery, Kubernetes, or Docker:
same lesson, a tenth of the machinery, and you can read the whole job store
with `sqlite3`.

## Still out of scope

Multi-tenancy beyond the isolation GitHub's installation model gives you free.
RBAC. Embeddings. Async inside a single review — parallelism belongs across
jobs, in the worker, not inside one loop.
