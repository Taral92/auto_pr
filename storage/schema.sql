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
        -- queued | running | post_pending | published | degraded | failed
        -- | cancelled | superseded
        -- post_pending: the review is COMPUTED AND PERSISTED, only the GitHub
        -- post is outstanding. Claiming such a row re-posts; it never re-runs
        -- the model.
    attempts           INTEGER     NOT NULL DEFAULT 0,
    leased_until       TIMESTAMPTZ,
    not_before         TIMESTAMPTZ,   -- earliest retry; set by a delayed
                                      -- requeue so a rate-limited run waits
                                      -- instead of being re-claimed at once
    worker_id          TEXT,
    cancel             BOOLEAN     NOT NULL DEFAULT FALSE,
    error              TEXT,       -- TRANSIENT: the failure currently being
                                   -- retried, cleared once it is resolved.
    degraded_reason    TEXT,       -- DURABLE: why the review was cut short
                                   -- (budget_breach:tokens, submit_failed:...).
                                   -- Two columns because `error` alone carried
                                   -- both meanings and they have opposite
                                   -- lifetimes: closing out a post cleared the
                                   -- transient error and took the degradation
                                   -- reason with it, and a post retry
                                   -- overwrote the reason before that. Written
                                   -- only when the run is degraded, so an
                                   -- ordinary failure never reads as one.

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

-- Idempotent migration for databases created before delayed retries existed.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS not_before TIMESTAMPTZ;
-- ...and before a degraded review kept its reason past the post.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS degraded_reason TEXT;

-- Backfill: on a database written by the old code, a degraded run that had not
-- yet been closed out still has its reason in `error`, where that code left it.
-- Move it across so the deploy does not strand those rows.
--
-- Idempotent three times over, because this runs on every boot: the predicate
-- stops matching once the column is populated, `degraded_reason IS NULL` means
-- an already-populated value is never overwritten, and `state = 'degraded'`
-- means an error on a failed or published run is never promoted into a
-- degradation reason - which would claim something the run never did.
--
-- Not recoverable, and not attempted: a legacy degraded run whose post already
-- completed had `error` set to NULL by the old `mark_posted`. That reason is
-- gone from the database and nothing here can invent it.
UPDATE runs
   SET degraded_reason = error
 WHERE state = 'degraded'
   AND degraded_reason IS NULL
   AND error IS NOT NULL;
