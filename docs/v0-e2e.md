# v0 — end-to-end working slice

Goal: PR URL in -> real review comment out -> visible in a UI.
Three slices. Each one runs before the next starts.

Repo layout after v0:

```
auto-pr/
  agent/        existing loop, tools, models   (unchanged shape)
  api/          FastAPI
    main.py
    db.py
    schema.sql
  web/          Next.js
  scripts/
  docs/
```

---

# SLICE 1 — the loop, end to end (no server)

`python -m agent.cli review <pr-url> [--dry-run]`

Follow `docs/b1-core-review.md` steps 1-10. Summary:

1. `GET /repos/{o}/{r}/pulls/{n}` -> `head.sha`
2. Same endpoint, `Accept: application/vnd.github.v3.diff` -> diff text
3. `git init` / `remote add` / `fetch --depth=1 origin pull/{n}/head` / `checkout FETCH_HEAD`
   into `tempfile.mkdtemp()`, deleted in `finally`
4. Run the existing loop with `repo_root` = that dir
5. Ground: evidence must be an exact substring of (diff + all tool_results)
6. Anchor: parse `@@` hunks -> anchorable line set -> inline vs summary
7. `POST /repos/{o}/{r}/pulls/{n}/reviews`
8. Write `runs/*.json`

Core function must be server-agnostic — Slice 2 imports it:

```
def review_pr(owner, repo, number, token, dry_run=False) -> ReviewResult
```

## Required in Slice 1 (these are not hardening — without them it does not run)

- **Tool errors return as `tool_result`, never exceptions.**
  `search_code` dies on the first PNG in any real repo. `read_file` raises on a
  path the model guessed. Bad regex raises `re.error`. Catch, stringify, return.
- **`list_files` ignore list.** Skip `.git`, `.venv`, `node_modules`, `dist`,
  `build`, `__pycache__`, lockfiles. Cap entries. One call on a Node repo is
  otherwise a context bomb.
- **`read_file` output cap** (~60 KB) with an explicit `[truncated: N more lines]`
  marker so the model knows it did not see everything.
- **Never log the token.** Redact the remote URL before printing anything.

## Deferred to after the UI works

- Path jail (H1) — needed before you point this at repos that aren't yours
- Wall-clock / cost ceiling (H4)
- Idempotency key (H5)

## Done when

- `--dry-run` prints a valid review payload with at least one anchored comment
- Real run posts a real inline comment on a real PR
- Temp dir gone afterwards — verify

---

# SLICE 2 — FastAPI

`api/` — thin. It calls `review_pr()`, it does not reimplement anything.

## DB — 2 tables only (not 5; installations/repos come with the App later)

```sql
CREATE TABLE runs (
  id            TEXT PRIMARY KEY,
  pr_url        TEXT NOT NULL,
  owner         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  pr_number     INTEGER NOT NULL,
  head_sha      TEXT,
  state         TEXT NOT NULL,      -- queued|running|published|degraded|failed
  error         TEXT,
  prompt_sha    TEXT,
  model         TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  wall_clock_s  REAL,
  grounded      INTEGER,
  near          INTEGER,
  ungrounded    INTEGER,
  inline        INTEGER,
  summary       INTEGER,
  trace_json    TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE findings (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(id),
  severity    TEXT NOT NULL,        -- blocker|should_fix|nit
  category    TEXT NOT NULL,        -- correctness|security|performance|maintainability|test_gap
  path        TEXT NOT NULL,
  line        INTEGER,              -- computed by us, NULL if summary-only
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  evidence    TEXT NOT NULL,
  verdict     TEXT NOT NULL,        -- grounded|near|ungrounded
  anchored    TEXT NOT NULL,        -- inline|summary|dropped
  posted      INTEGER NOT NULL
);
```

Raw `sqlite3`. No ORM. `api/schema.sql` applied on startup if tables missing.

## Endpoints

| Method | Path | Does |
|---|---|---|
| `POST` | `/api/review` | body `{pr_url, dry_run}` -> insert run (state=queued), kick off background, return `{run_id}` |
| `GET`  | `/api/runs` | list, newest first |
| `GET`  | `/api/runs/{id}` | run + its findings + trace |

- Background execution: FastAPI `BackgroundTasks` for now.
  **Known limitation:** dies with the process, no retry, no restart survival.
  Replaced by the SQLite worker at B2. Do not build the worker yet.
- Update `runs.state` as it progresses so the UI can poll.
- Run on port 8000.

---

# SLICE 3 — Next.js

`web/` — App Router, TypeScript, Tailwind. Port 3000.

## Avoid CORS entirely

`next.config.js`:

```js
async rewrites() {
  return [{ source: '/api/:path*', destination: 'http://localhost:8000/api/:path*' }]
}
```

Frontend calls `/api/...` — same origin, no CORS config anywhere.

## Pages — 3, no more

**`/`  — New review**
- input: PR URL
- checkbox: dry run
- button: Review
- on submit -> `POST /api/review` -> redirect to `/runs/{id}`

**`/runs` — History**
- table: PR, state, findings count, grounded/dropped, duration, cost, time
- state as a colored chip
- row click -> detail

**`/runs/[id]` — Detail**
- header: PR link, state, head_sha, model, tokens, duration
- **Findings** list: severity chip, category, `path:line`, title, body,
  evidence in a `<pre>`, and `anchored` badge (inline / summary / dropped)
- **Grounding summary**: grounded / near / ungrounded counts
- **Trace**: per iteration — tool name, input, result length, tokens
- poll every 2s while `state` is `queued` or `running`

## Do NOT build

- No diff viewer. GitHub owns that.
- No auth, no login. Local only for now.
- No settings page yet.

---

# Order

```
1. Slice 1  ->  a real comment appears on a real PR
2. Slice 2  ->  curl POST /api/review does the same thing
3. Slice 3  ->  do it from the browser and read the trace
```

Do not start Slice 2 until Slice 1 posts a comment.
