CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  pr_url        TEXT NOT NULL,
  owner         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  pr_number     INTEGER NOT NULL,
  head_sha      TEXT,
  dry_run       INTEGER NOT NULL DEFAULT 0,

  state         TEXT NOT NULL,
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

  grounded      INTEGER,
  near          INTEGER,
  ungrounded    INTEGER,
  inline        INTEGER,
  summary       INTEGER,
  dropped       INTEGER,

  corpus_json   TEXT,
  trace_json    TEXT,
  payload_json  TEXT,
  created_at    REAL NOT NULL,
  started_at    REAL,
  finished_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_runs_claim ON runs(state, created_at);

CREATE TABLE IF NOT EXISTS findings (
  id        TEXT PRIMARY KEY,
  run_id    TEXT NOT NULL REFERENCES runs(id),
  severity  TEXT NOT NULL,
  category  TEXT NOT NULL,
  path      TEXT NOT NULL,
  line      INTEGER,
  title     TEXT NOT NULL,
  body      TEXT NOT NULL,
  evidence  TEXT NOT NULL,
  verdict   TEXT NOT NULL,
  anchored  TEXT NOT NULL,
  posted    INTEGER NOT NULL DEFAULT 0
);
