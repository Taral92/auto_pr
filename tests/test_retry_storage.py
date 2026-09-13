"""The storage half of the retry lifecycle. Needs local Postgres.

Skipped against a remote DATABASE_URL so pytest cannot write to a live
database - the same guard test_coalesce.py uses, and for the same reason.

What these pin is the part the FakeStore tests in test_retry.py cannot reach:
the SQL. `not_before` is the whole backoff mechanism, `prior_state` is how the
worker knows it claimed a finished review rather than fresh work, and
`requeue_post` is what keeps a completed review out of the model pipeline.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config import get_settings
from storage import runs as R
from storage.db import close_pool, init_db, pool

LEASE = 900
WORKER = "test-worker"
MAX_ATTEMPTS = 3


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv(
        "ANTHROPIC_API_KEY", os.environ.get("ANTHROPIC_API_KEY") or "test"
    )
    monkeypatch.setenv("GITHUB_TOKEN", os.environ.get("GITHUB_TOKEN") or "test")
    get_settings.cache_clear()
    close_pool()
    url = get_settings().database_url
    if not any(h in url for h in ("localhost", "127.0.0.1")):
        pytest.skip("retry storage tests need local postgres")
    init_db()
    yield
    close_pool()
    get_settings.cache_clear()


@pytest.fixture
def run(db):
    """One queued run, owned by a unique owner so parallel runs cannot collide."""
    owner = f"retry-{uuid.uuid4().hex[:8]}"
    run_id = R.insert_queued(
        pr_url="https://github.com/o/r/pull/1", owner=owner, repo="r",
        pr_number=1, head_sha="a" * 40, delivery_id=str(uuid.uuid4()),
    )
    yield run_id, owner
    with pool().connection() as conn:
        conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def _set(run_id, **cols):
    """Set columns to real Python values - never SQL fragments as parameters."""
    sets = ", ".join(f"{k}=%s" for k in cols)
    with pool().connection() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id=%s",
                     (*cols.values(), run_id))


def _in(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


def _row(run_id):
    return R.get_run(run_id)


def _claim_ids(limit=6):
    """Claim repeatedly and collect the ids, so one test can assert on its own
    run without being confused by other rows in a shared database."""
    seen = []
    for _ in range(limit):
        row = R.claim(lease_s=LEASE, worker_id=WORKER, max_attempts=MAX_ATTEMPTS)
        if row is None:
            break
        seen.append(row)
    return seen


# -- 1 + 2: not_before gates the claim ------------------------------------


def test_a_future_not_before_blocks_a_queued_claim(run):
    run_id, _ = run
    _set(run_id, not_before=_in(hours=1))
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_a_future_not_before_blocks_a_post_pending_claim(run):
    run_id, _ = run
    _set(run_id, state="post_pending")
    _set(run_id, not_before=_in(hours=1))
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_a_past_not_before_is_claimable(run):
    run_id, _ = run
    _set(run_id, not_before=_in(seconds=-1))
    assert run_id in [r["id"] for r in _claim_ids()]


def test_a_null_not_before_is_claimable(run):
    """The common case: a run that has never been requeued."""
    run_id, _ = run
    assert _row(run_id)["not_before"] is None
    assert run_id in [r["id"] for r in _claim_ids()]


def test_requeue_with_a_delay_sets_not_before_in_the_future(run):
    run_id, _ = run
    _set(run_id, state="running", worker_id=WORKER)   # requeue needs the lease
    assert R.requeue(run_id, error="429", delay_s=120, worker_id=WORKER) is True
    row = _row(run_id)
    assert row["state"] == "queued"
    assert row["not_before"] is not None
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_requeue_without_a_delay_is_immediately_claimable(run):
    """Preserves the old behaviour when no delay is asked for.

    The requeue's own return value is asserted: a fenced-out requeue is a
    no-op, and a no-op leaves the row queued and claimable - so without this
    the test would pass whether or not the requeue ever ran.
    """
    run_id, _ = run
    _set(run_id, state="running", worker_id=WORKER)
    assert R.requeue(run_id, error="blip", worker_id=WORKER) is True
    assert run_id in [r["id"] for r in _claim_ids()]


# -- 3: claim returns the prior state ------------------------------------


def test_claim_reports_prior_state_queued(run):
    run_id, _ = run
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "queued"
    assert claimed[run_id]["state"] == "running"     # the claim overwrote it


def test_claim_reports_prior_state_post_pending(run):
    """Without this the worker cannot tell a finished review from fresh work,
    and would re-run the model pipeline it has already paid for."""
    run_id, _ = run
    _set(run_id, state="post_pending")
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "post_pending"


def test_a_dead_lease_is_still_reclaimed(run):
    run_id, _ = run
    _set(run_id, state="running")
    _set(run_id, leased_until=_in(minutes=-1))
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "running"


# -- 4: requeue_post keeps the computed review --------------------------


PAYLOAD = {"event": "COMMENT", "body": "b <!-- auto-pr:key -->", "comments": []}


def _persisted(run_id, worker_id=WORKER):
    """A review computed and persisted by a worker that still holds the lease.

    `worker_id` is part of that state, not decoration: every mutation below is
    ownership-fenced, so a row with no owner is a row nobody may close out.
    """
    with pool().connection() as conn:
        conn.execute(
            """UPDATE runs SET state='post_pending', payload=%s, model=%s,
                               worker_id=%s
                WHERE id=%s""",
            (json.dumps(PAYLOAD), "gpt-5.6-luna", worker_id, run_id),
        )


def test_requeue_post_keeps_state_and_payload(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="GitHub 429 secondary limit", delay_s=45,
                   worker_id=WORKER)

    row = _row(run_id)
    assert row["state"] == "post_pending"        # not 'queued': no re-review
    assert row["payload"] == PAYLOAD             # the completed work survives
    assert row["model"] == "gpt-5.6-luna"
    assert row["not_before"] is not None
    assert row["worker_id"] is None and row["leased_until"] is None


def test_a_requeued_post_is_not_claimable_until_its_time(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="429", delay_s=3600, worker_id=WORKER)
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_a_requeued_post_is_claimable_once_due_and_routes_as_post_pending(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="429", delay_s=0, worker_id=WORKER)
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "post_pending"
    assert claimed[run_id]["payload"] == PAYLOAD


# -- 5: mark_posted closes the run out ----------------------------------


def _finding(run_id, anchored):
    with pool().connection() as conn:
        conn.execute(
            """INSERT INTO findings (id, run_id, severity, category, path, line,
                   title, body, evidence, verdict, anchored, posted)
               VALUES (%s,%s,'nit','maintainability','a.py',1,'t','b','e',
                       'grounded',%s,FALSE)""",
            (str(uuid.uuid4()), run_id, anchored),
        )


def test_mark_posted_moves_post_pending_to_published(run):
    run_id, _ = run
    _persisted(run_id)
    R.mark_posted(run_id, state="published", posted=True,
                  worker_id=WORKER)

    row = _row(run_id)
    assert row["state"] == "published"
    assert row["finished_at"] is not None
    assert row["not_before"] is None
    assert row["error"] is None


def test_mark_posted_moves_running_to_degraded(run):
    """A degraded review still publishes; the state must survive the post."""
    run_id, _ = run
    _set(run_id, state="running", worker_id=WORKER)
    R.mark_posted(run_id, state="degraded", posted=True, worker_id=WORKER)
    assert _row(run_id)["state"] == "degraded"


def test_mark_posted_flags_only_the_findings_that_reached_github(run):
    run_id, _ = run
    _persisted(run_id)
    _finding(run_id, "inline")
    _finding(run_id, "summary")
    _finding(run_id, "dropped")
    R.mark_posted(run_id, state="published", posted=True,
                  worker_id=WORKER)

    by_anchor = {f["anchored"]: f["posted"] for f in R.findings_for(run_id)}
    assert by_anchor["inline"] is True
    assert by_anchor["summary"] is True
    assert by_anchor["dropped"] is False      # never reached a human


def test_mark_posted_with_posted_false_leaves_findings_unposted(run):
    """dry_run, or a review GitHub already had."""
    run_id, _ = run
    _persisted(run_id)
    _finding(run_id, "inline")
    R.mark_posted(run_id, state="published", posted=False,
                  worker_id=WORKER)
    assert all(f["posted"] is False for f in R.findings_for(run_id))


def test_a_closed_out_run_is_no_longer_claimable(run):
    run_id, _ = run
    _persisted(run_id)
    R.mark_posted(run_id, state="published", posted=True,
                  worker_id=WORKER)
    assert run_id not in [r["id"] for r in _claim_ids()]


# -- 6: the migration is idempotent -------------------------------------


def test_schema_applies_twice_without_error(db):
    """schema.sql runs on every worker boot."""
    init_db()
    init_db()


def test_the_not_before_column_exists(db):
    with pool().connection() as conn:
        found = conn.execute(
            """SELECT data_type FROM information_schema.columns
                WHERE table_name='runs' AND column_name='not_before'"""
        ).fetchone()
    assert found and "timestamp" in found["data_type"]


def test_the_migration_adds_the_column_to_a_pre_existing_table(db):
    """Simulates a database created before delayed retries existed: CREATE TABLE
    IF NOT EXISTS would not add the column, so schema.sql carries an ALTER."""
    with pool().connection() as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN IF EXISTS not_before")
        gone = conn.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_name='runs' AND column_name='not_before'"""
        ).fetchone()
    assert gone is None

    init_db()                                  # re-apply schema.sql

    with pool().connection() as conn:
        back = conn.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_name='runs' AND column_name='not_before'"""
        ).fetchone()
    assert back is not None


def test_the_claim_index_still_matches_the_claim_predicate(db):
    """The partial index must cover the states the claim actually looks for."""
    with pool().connection() as conn:
        ddl = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='idx_runs_claim'"
        ).fetchone()
    assert ddl is not None


# -- the attempt ceiling, enforced in SQL --------------------------------
#
# `attempts` increments in CLAIM_SQL, so the row a worker holds already counts
# the execution it is about to perform: fresh = 0, first claim returns 1. With
# max_attempts=3 the predicate `attempts < 3` admits 0, 1 and 2 - three
# executions - and the worker's own `attempts < max_attempts` guard
# dead-letters on the third. The two agree by construction.
#
# The ceiling has to live in SQL as well as the worker because the worker's
# guard is inside an `except` block. A SIGKILL, an OOM kill or a container
# eviction never reaches it: the row stays `running`, the lease expires, and
# before this it was claimable again forever - unbounded spend on one bad row.


def _claim():
    return R.claim(lease_s=LEASE, worker_id=WORKER, max_attempts=MAX_ATTEMPTS)


@pytest.mark.parametrize("attempts", [0, 1, 2])
def test_a_run_below_the_ceiling_is_claimable(run, attempts):
    run_id, _ = run
    _set(run_id, attempts=attempts)
    assert run_id in [r["id"] for r in _claim_ids()]


@pytest.mark.parametrize("attempts", [3, 4, 99])
def test_a_run_at_or_over_the_ceiling_is_not_claimable(run, attempts):
    run_id, _ = run
    _set(run_id, attempts=attempts)
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_the_claim_increments_attempts(run):
    run_id, _ = run
    assert _row(run_id)["attempts"] == 0
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["attempts"] == 1
    assert _row(run_id)["attempts"] == 1


def test_three_executions_are_permitted_then_no_more(run):
    """What max_attempts=3 actually buys, end to end."""
    run_id, _ = run
    for expected in (1, 2, 3):
        claimed = {r["id"]: r for r in _claim_ids()}
        assert claimed[run_id]["attempts"] == expected
        R.requeue(run_id, error="blip", worker_id=WORKER)   # queued, attempts kept
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_an_expired_running_row_below_the_ceiling_is_still_reclaimed(run):
    """Normal lease reclamation must survive the new predicate."""
    run_id, _ = run
    _set(run_id, state="running", attempts=1, leased_until=_in(minutes=-1))
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "running"
    assert claimed[run_id]["attempts"] == 2


def test_an_expired_running_row_at_the_ceiling_is_not_reclaimed(run):
    """The crash-loop case: before this it was claimable forever."""
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS, leased_until=_in(minutes=-1))
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_not_before_still_gates_a_run_below_the_ceiling(run):
    run_id, _ = run
    _set(run_id, attempts=1, not_before=_in(hours=1))
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_post_pending_below_the_ceiling_is_still_claimable(run):
    """C2's retry path must not be closed by the ceiling."""
    run_id, _ = run
    _persisted(run_id)
    _set(run_id, attempts=1)
    claimed = {r["id"]: r for r in _claim_ids()}
    assert claimed[run_id]["prior_state"] == "post_pending"
    assert claimed[run_id]["payload"] == PAYLOAD


def test_post_pending_at_the_ceiling_is_not_claimable(run):
    run_id, _ = run
    _persisted(run_id)
    _set(run_id, attempts=MAX_ATTEMPTS)
    assert run_id not in [r["id"] for r in _claim_ids()]


# -- the reaper ----------------------------------------------------------


def test_an_abandoned_run_at_the_ceiling_is_reaped_to_failed(run):
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS,
         leased_until=_in(minutes=-1), worker_id="dead-worker")

    assert run_id in R.reap_exhausted(max_attempts=MAX_ATTEMPTS)

    row = _row(run_id)
    assert row["state"] == "failed"
    assert row["error"] == R.ATTEMPTS_EXHAUSTED == "attempts exhausted"
    assert row["finished_at"] is not None
    assert row["leased_until"] is None and row["worker_id"] is None


def test_a_reaped_run_cannot_be_claimed_again(run):
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS, leased_until=_in(minutes=-1))
    R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_the_reaper_leaves_a_live_lease_alone(run):
    """A worker that is alive and heartbeating must not be shot."""
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS, leased_until=_in(minutes=5))
    assert run_id not in R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    assert _row(run_id)["state"] == "running"


def test_the_reaper_leaves_a_run_below_the_ceiling_alone(run):
    """That row is still retryable; reaping it would lose a real review."""
    run_id, _ = run
    _set(run_id, state="running", attempts=1, leased_until=_in(minutes=-1))
    assert run_id not in R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    assert _row(run_id)["state"] == "running"


def test_the_reaper_ignores_queued_and_finished_rows(run):
    run_id, _ = run
    _set(run_id, attempts=MAX_ATTEMPTS)            # queued, no lease
    assert run_id not in R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    assert _row(run_id)["state"] == "queued"


def test_the_reaper_is_idempotent(run):
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS, leased_until=_in(minutes=-1))
    first = R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    second = R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    assert run_id in first and run_id not in second


def test_the_reaper_does_not_resurrect_or_retry(run):
    """It dead-letters; it must never put the row back in the queue."""
    run_id, _ = run
    _set(run_id, state="running", attempts=MAX_ATTEMPTS, leased_until=_in(minutes=-1))
    R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    row = _row(run_id)
    assert row["state"] == "failed"
    assert row["not_before"] is None
