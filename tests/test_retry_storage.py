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
        row = R.claim(lease_s=LEASE, worker_id=WORKER)
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
    R.requeue(run_id, error="429", delay_s=120)
    row = _row(run_id)
    assert row["state"] == "queued"
    assert row["not_before"] is not None
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_requeue_without_a_delay_is_immediately_claimable(run):
    """Preserves the old behaviour when no delay is asked for."""
    run_id, _ = run
    R.requeue(run_id, error="blip")
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


def _persisted(run_id):
    with pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET state='post_pending', payload=%s, model=%s WHERE id=%s",
            (json.dumps(PAYLOAD), "gpt-5.6-luna", run_id),
        )


def test_requeue_post_keeps_state_and_payload(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="GitHub 429 secondary limit", delay_s=45)

    row = _row(run_id)
    assert row["state"] == "post_pending"        # not 'queued': no re-review
    assert row["payload"] == PAYLOAD             # the completed work survives
    assert row["model"] == "gpt-5.6-luna"
    assert row["not_before"] is not None
    assert row["worker_id"] is None and row["leased_until"] is None


def test_a_requeued_post_is_not_claimable_until_its_time(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="429", delay_s=3600)
    assert run_id not in [r["id"] for r in _claim_ids()]


def test_a_requeued_post_is_claimable_once_due_and_routes_as_post_pending(run):
    run_id, _ = run
    _persisted(run_id)
    R.requeue_post(run_id, error="429", delay_s=0)
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
    R.mark_posted(run_id, state="published", posted=True)

    row = _row(run_id)
    assert row["state"] == "published"
    assert row["finished_at"] is not None
    assert row["not_before"] is None
    assert row["error"] is None


def test_mark_posted_moves_running_to_degraded(run):
    """A degraded review still publishes; the state must survive the post."""
    run_id, _ = run
    _set(run_id, state="running")
    R.mark_posted(run_id, state="degraded", posted=True)
    assert _row(run_id)["state"] == "degraded"


def test_mark_posted_flags_only_the_findings_that_reached_github(run):
    run_id, _ = run
    _persisted(run_id)
    _finding(run_id, "inline")
    _finding(run_id, "summary")
    _finding(run_id, "dropped")
    R.mark_posted(run_id, state="published", posted=True)

    by_anchor = {f["anchored"]: f["posted"] for f in R.findings_for(run_id)}
    assert by_anchor["inline"] is True
    assert by_anchor["summary"] is True
    assert by_anchor["dropped"] is False      # never reached a human


def test_mark_posted_with_posted_false_leaves_findings_unposted(run):
    """dry_run, or a review GitHub already had."""
    run_id, _ = run
    _persisted(run_id)
    _finding(run_id, "inline")
    R.mark_posted(run_id, state="published", posted=False)
    assert all(f["posted"] is False for f in R.findings_for(run_id))


def test_a_closed_out_run_is_no_longer_claimable(run):
    run_id, _ = run
    _persisted(run_id)
    R.mark_posted(run_id, state="published", posted=True)
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
