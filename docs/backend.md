# Backend — FastAPI + worker

Goal: `review_pr()` runs behind an HTTP API, survives restarts, and reports
state the UI can poll.

## Layout (this restructure is the point)

```
auto_pr/
  config.py            one Settings object
  core/
    models.py          Finding, GroundedFinding, ReviewResult  (no I/O)
    errors.py          the error taxonomy
  agent/               graph, loop, tools, prompts
  gh/                  GitHub: client, diff parse, anchor, publish
  storage/
    db.py              connection, pragmas, migrations
    schema.sql
    runs.py            queries
  api/
    main.py            app + lifespan
    routes.py
    schemas.py         request/response models (NOT domain models)
  worker/
    main.py            claim -> run -> record loop
```

`gh/` moving out of `agent/` is the single most important change.
The agent must not know GitHub exists — it gets a diff and a directory.

---

## config.py

```python
class Settings(BaseSettings):
    anthropic_api_key: SecretStr
    github_token: SecretStr
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 4096
    max_iterations: int = 10
    max_wall_clock_s: int = 300
    max_tokens_total: int = 200_000
    max_diff_bytes: int = 400_000
    db_path: str = "auto_pr.db"
    model_config = SettingsConfigDict(env_file=".env")
```

`SecretStr` so a token can never land in a traceback or a log line.

Delete the duplicated model fallback in `loop.py:32` and `review.py:34`.
One source of truth or they will drift.

---

## core/errors.py — the "handle better" part

```python
class AutoPrError(Exception): ...

class TransientError(AutoPrError):    # retry w/ backoff: 429, 5xx, network, git timeout
    ...
class PermanentError(AutoPrError):    # dead-letter, no retry: 404, bad URL, auth, path-jail hit
    ...
class BudgetExceeded(AutoPrError):    # degrade: publish partial + reason
    ...
class DiffTooLarge(AutoPrError):      # publish body-only notice
    ...
```

Every raise site picks one. The worker maps them to states:

| Exception | Job state | Retry? |
|---|---|---|
| `TransientError` | `queued` (attempts+1) | yes, capped |
| `PermanentError` | `failed` | no |
| `BudgetExceeded` | `degraded` | no, still publishes |
| `DiffTooLarge` | `published` | no |
| anything else | `failed` | no — and it is a bug, log loudly |

A bare `except Exception` anywhere that swallows is a defect.

---

## storage — ONE table doubles as the queue

Do not build separate `jobs` and `runs` tables yet. A job IS a run.

```sql
CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  pr_url        TEXT NOT NULL,
  owner         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  pr_number     INTEGER NOT NULL,
  head_sha      TEXT,
  dry_run       INTEGER NOT NULL DEFAULT 0,

  state         TEXT NOT NULL,   -- queued|running|published|degraded|failed|cancelled
  attempts      INTEGER NOT NULL DEFAULT 0,
  leased_until  REAL,
  worker_id     TEXT,
  cancel        INTEGER NOT NULL DEFAULT 0,
  error         TEXT,

  prompt_sha    TEXT,
  model         TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  wall_clock_s  REAL,

  grounded      INTEGER, near INTEGER, ungrounded INTEGER,
  inline        INTEGER, summary INTEGER, dropped INTEGER,

  corpus_json   TEXT,            -- persist it. you could not audit the last run.
  trace_json    TEXT,
  payload_json  TEXT,
  created_at    REAL NOT NULL,
  finished_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs(state, created_at);

CREATE TABLE IF NOT EXISTS findings (
  id        TEXT PRIMARY KEY,
  run_id    TEXT NOT NULL REFERENCES runs(id),
  severity  TEXT NOT NULL,      -- blocker|should_fix|nit
  category  TEXT NOT NULL,      -- correctness|security|performance|maintainability|test_gap
  path      TEXT NOT NULL,
  line      INTEGER,
  title     TEXT NOT NULL,
  body      TEXT NOT NULL,
  evidence  TEXT NOT NULL,
  verdict   TEXT NOT NULL,      -- grounded|near|ungrounded
  anchored  TEXT NOT NULL,      -- inline|summary|dropped
  posted    INTEGER NOT NULL DEFAULT 0
);
```

`corpus_json` is not optional. Without it you cannot tell a correct drop from
a broken gate — which is exactly the question you could not answer today.

### SQLite pragmas — set these or the API stalls behind the worker

```python
conn.execute("PRAGMA journal_mode=WAL")     # readers don't block on the writer
conn.execute("PRAGMA busy_timeout=5000")
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA synchronous=NORMAL")
```

One connection per thread. Never share a connection across threads.

### The claim query (this is the whole concurrency story)

```sql
UPDATE runs
   SET state='running', leased_until=:now+:lease, worker_id=:wid, attempts=attempts+1
 WHERE id = (
       SELECT id FROM runs
        WHERE state='queued'
           OR (state='running' AND leased_until < :now)   -- reclaim dead lease
        ORDER BY created_at
        LIMIT 1)
RETURNING id;
```

- Atomic. Two workers cannot claim the same row.
- Lease MUST exceed `max_wall_clock_s`, or a slow run gets reclaimed and
  reviewed twice.
- Requires SQLite >= 3.35 for `RETURNING`. Check at startup, fail loudly if older.

---

## api/

| Method | Path | Returns |
|---|---|---|
| `POST` | `/api/review` | `202 {run_id}` — insert `state=queued`, return immediately |
| `GET` | `/api/runs` | list, newest first, `?limit=&cursor=` |
| `GET` | `/api/runs/{id}` | run + findings (no trace — it is large) |
| `GET` | `/api/runs/{id}/trace` | trace + corpus |
| `POST` | `/api/runs/{id}/cancel` | sets `cancel=1` |
| `GET` | `/healthz` | db reachable, sqlite version, queued/running counts |

Rules:
- **The API never runs a review.** It only writes rows. No `BackgroundTasks`.
- `api/schemas.py` request/response models are separate from `core/models.py`.
  Never return a domain object directly — you will leak fields.
- Validate the PR URL at the boundary, return `422` on a bad one.
- Port 8000.

## worker/

```
loop:
  row = claim()
  if not row: sleep(2); continue
  try:
      result = review_pr(...)          # unchanged signature
      record(published|degraded)
  except TransientError:  requeue if attempts < N else failed
  except PermanentError:  failed
  except BudgetExceeded:  degraded
  finally:                always write finished_at
```

- `worker_id` = hostname + pid. Makes a stuck lease traceable to a process.
- Cancellation: check `cancel` flag between graph nodes, raise, mark `cancelled`.
- Run as a separate process: `python -m worker.main`

---

## Done when

1. `POST /api/review` returns in < 100ms with a `run_id`
2. Worker picks it up and the row reaches `published` or `degraded`
3. `kill -9` the worker mid-run -> lease expires -> another worker reclaims it
4. Two workers running: no row is ever claimed twice
5. `GET /api/runs/{id}/trace` returns the corpus, so gate decisions are auditable
6. No token appears in any log line or traceback

## Not in this slice

- No webhook, no App auth
- No coalescing or idempotency
- No frontend
