-- auto-pr schema (PostgreSQL)
-- Idempotent: safe to run on every boot.

CREATE TABLE IF NOT EXISTS installations (
    id                     TEXT PRIMARY KEY,
    gh_installation_id     BIGINT      NOT NULL UNIQUE,
    account_login          TEXT        NOT NULL,
    account_type           TEXT,
    suspended              BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id                 TEXT PRIMARY KEY,
    installation_id    BIGINT,
    delivery_id        TEXT UNIQUE,          -- GitHub's X-GitHub-Delivery.
                                             -- UNIQUE is the cheapest possible
                                             -- redelivery guard: the INSERT
                                             -- conflicts before any work starts.
    pr_url             TEXT        NOT NULL,
    owner              TEXT        NOT NULL,
    repo               TEXT        NOT NULL,
    pr_number          INTEGER     NOT NULL,
    head_sha           TEXT,
    dry_run            BOOLEAN     NOT NULL DEFAULT FALSE,

    state              TEXT        NOT NULL,
        -- queued | running | published | degraded | failed | cancelled | superseded
    attempts           INTEGER     NOT NULL DEFAULT 0,
    leased_until       TIMESTAMPTZ,
    worker_id          TEXT,
    cancel             BOOLEAN     NOT NULL DEFAULT FALSE,
    error              TEXT,

    prompt_sha         TEXT,
    model              TEXT,
    tokens_in          INTEGER,
    tokens_out         INTEGER,
    cache_read_tokens  INTEGER,
    wall_clock_s       DOUBLE PRECISION,

    grounded           INTEGER,
    near               INTEGER,
    ungrounded         INTEGER,
    inline             INTEGER,
    summary            INTEGER,
    dropped            INTEGER,

    corpus             JSONB,
    trace              JSONB,
    payload            JSONB,

    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ
);

-- The claim query orders queued work by age; this is the index it rides.
CREATE INDEX IF NOT EXISTS idx_runs_claim
    ON runs (state, created_at)
    WHERE state IN ('queued', 'running');

-- Coalescing looks up every live job for one PR.
CREATE INDEX IF NOT EXISTS idx_runs_pr
    ON runs (owner, repo, pr_number)
    WHERE state IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at DESC);

CREATE TABLE IF NOT EXISTS findings (
    id         TEXT PRIMARY KEY,
    run_id     TEXT        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    severity   TEXT        NOT NULL,   -- blocker | should_fix | nit
    category   TEXT        NOT NULL,   -- correctness | security | performance
                                       -- | maintainability | test_gap
    path       TEXT        NOT NULL,
    line       INTEGER,
    title      TEXT        NOT NULL,
    body       TEXT        NOT NULL,
    evidence   TEXT        NOT NULL,
    verdict    TEXT        NOT NULL,   -- grounded | near | ungrounded
    anchored   TEXT        NOT NULL,   -- inline | summary | dropped
    posted     BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON findings (run_id);
