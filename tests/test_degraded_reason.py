"""A degraded review keeps its reason once it is published.

`error` used to carry two meanings with opposite lifetimes: the TRANSIENT
failure currently being retried, and the DURABLE reason a review was cut short.
Closing out a post clears the first, and that took the second with it - leaving
`state='degraded'` with nothing saying which budget went, which is the one field
that makes the state actionable.

Two losses, one cause:

* degraded -> post succeeds: `mark_posted` set `error=NULL`.
* degraded -> post fails once: `requeue_post` overwrote `error` with the post
  failure BEFORE that, and `post_pending` then hardcoded `state='published'`,
  so the run was recorded as a clean publish.

`degraded_reason` is a separate column with the durable lifetime. It is written
only when the run is degraded, so an outright failure never reads as one.
"""

import json
import os
import uuid

import pytest

from config import get_settings
from storage import runs as R
from storage.db import close_pool, init_db, pool

LEASE = 900
MAX_ATTEMPTS = 3
WORKER = "reason-worker"
BUDGET = "budget_breach:tokens"


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
        pytest.skip("degraded-reason tests need local postgres")
    init_db()
    yield
    close_pool()
    get_settings.cache_clear()


@pytest.fixture
def run(db):
    owner = f"reason-{uuid.uuid4().hex[:8]}"
    run_id = R.insert_queued(
        pr_url="https://github.com/o/r/pull/1", owner=owner, repo="r",
        pr_number=1, head_sha="a" * 40, delivery_id=str(uuid.uuid4()),
    )
    yield run_id, owner
    with pool().connection() as conn:
        conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def _claim(worker_id, owner):
    for _ in range(20):
        row = R.claim(lease_s=LEASE, worker_id=worker_id,
                      max_attempts=MAX_ATTEMPTS)
        if row is None:
            return None
        if row["owner"] == owner:
            return row
    return None


class _Result:
    """What review_pr hands back. `status` decides; `error` is the reason."""

    def __init__(self, status="published", error=None):
        self.status = status
        self.error = error

    head_sha = "b" * 40
    prompt_sha = "p"
    model = "m"
    tokens_in = 1
    tokens_out = 2
    wall_clock_s = 1.5
    grounding = {"grounded": 1, "near": 0, "ungrounded": 0}
    anchoring = {"inline": 1, "summary": 0, "dropped": 0}
    corpus: list = []
    trace: list = []
    payload: dict = {"body": "b"}
    findings: list = []


# -- 1: a normal review carries no reason ---------------------------------


def test_a_normal_review_is_published_with_no_degradation_reason(run):
    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result(), state="post_pending", worker_id=WORKER)
    R.mark_posted(run_id, state="published", posted=True, worker_id=WORKER)

    row = R.get_run(run_id)
    assert row["state"] == "published"
    assert row["degraded_reason"] is None
    assert row["error"] is None


# -- 2: a degraded review keeps its reason through the post ---------------


def test_a_degraded_review_keeps_its_reason_after_being_posted(run):
    """The regression. This is what `mark_posted` used to erase."""
    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result("degraded", BUDGET),
                    state="post_pending", worker_id=WORKER)
    assert R.get_run(run_id)["degraded_reason"] == BUDGET

    R.mark_posted(run_id, state="degraded", posted=True, worker_id=WORKER)

    row = R.get_run(run_id)
    assert row["state"] == "degraded"
    assert row["degraded_reason"] == BUDGET    # survived the post
    assert row["error"] is None                # transient error still cleared


def test_the_reason_survives_a_post_retry_and_the_state_is_not_flattened(run):
    """The second loss: requeue_post overwrote the reason, then post_pending
    hardcoded 'published', so a degraded run was recorded as a clean one."""
    import worker.main as W

    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result("degraded", BUDGET),
                    state="post_pending", worker_id=WORKER)
    R.requeue_post(run_id, error="GitHub 429 secondary limit", delay_s=0,
                   worker_id=WORKER)

    mid = R.get_run(run_id)
    assert mid["error"] == "GitHub 429 secondary limit"   # transient, as before
    assert mid["degraded_reason"] == BUDGET               # durable, untouched

    row = _claim(WORKER, owner)
    assert row["prior_state"] == "post_pending"
    # The worker derives the terminal state from the durable column.
    final = "degraded" if row.get("degraded_reason") else "published"
    assert final == "degraded"
    R.mark_posted(run_id, state=final, posted=True, worker_id=WORKER)

    out = R.get_run(run_id)
    assert out["state"] == "degraded"
    assert out["degraded_reason"] == BUDGET
    assert out["error"] is None


def test_degrading_through_mark_also_records_the_reason(run):
    """However a run reaches `degraded`, the state carries a reason."""
    run_id, owner = run
    _claim(WORKER, owner)

    assert R.mark(run_id, "degraded", worker_id=WORKER,
                  error="budget_breach:seconds") is True
    row = R.get_run(run_id)
    assert row["state"] == "degraded"
    assert row["degraded_reason"] == "budget_breach:seconds"


# -- 3: a real error is never mistaken for a degradation ------------------


def test_a_failed_run_records_no_degradation_reason(run):
    run_id, owner = run
    _claim(WORKER, owner)

    R.mark(run_id, "failed", worker_id=WORKER, error="PermanentError: 404")

    row = R.get_run(run_id)
    assert row["state"] == "failed"
    assert row["error"] == "PermanentError: 404"
    assert row["degraded_reason"] is None


def test_a_failed_result_does_not_become_a_degradation_reason(run):
    """record_result keys on status, not on the presence of an error."""
    run_id, owner = run
    _claim(WORKER, owner)

    R.record_result(run_id, _Result("failed", "clone failed: auth"),
                    state="post_pending", worker_id=WORKER)

    row = R.get_run(run_id)
    assert row["error"] == "clone failed: auth"
    assert row["degraded_reason"] is None


def test_a_transient_post_failure_is_not_a_degradation_reason(run):
    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result(), state="post_pending", worker_id=WORKER)

    R.requeue_post(run_id, error="GitHub 503", delay_s=0, worker_id=WORKER)

    row = R.get_run(run_id)
    assert row["error"] == "GitHub 503"
    assert row["degraded_reason"] is None


def test_a_degraded_run_that_later_fails_outright_keeps_both_facts(run):
    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result("degraded", BUDGET),
                    state="post_pending", worker_id=WORKER)

    R.mark(run_id, "failed", worker_id=WORKER, error="post retries exhausted")

    row = R.get_run(run_id)
    assert row["state"] == "failed"
    assert row["error"] == "post retries exhausted"
    assert row["degraded_reason"] == BUDGET    # why it was degraded still known


def test_the_helper_keys_on_status_not_on_error():
    assert R.degraded_reason_of(_Result("published", None)) is None
    assert R.degraded_reason_of(_Result("failed", "boom")) is None
    assert R.degraded_reason_of(_Result("degraded", BUDGET)) == BUDGET
    # A degraded run with no message still records that it was degraded.
    assert R.degraded_reason_of(_Result("degraded", None)) == "unknown"


# -- 4 + 5: post_pending and fencing are untouched ------------------------


def test_post_pending_still_keeps_the_payload_and_does_not_re_review(run):
    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result("degraded", BUDGET),
                    state="post_pending", worker_id=WORKER)
    R.requeue_post(run_id, error="429", delay_s=0, worker_id=WORKER)

    row = _claim(WORKER, owner)
    assert row["prior_state"] == "post_pending"
    assert row["payload"] == {"body": "b"}      # the computed review survives


def test_the_reason_write_is_still_lease_fenced(run):
    """A stale worker cannot stamp a degradation reason on someone else's run."""
    run_id, owner = run
    _claim("worker-A", owner)
    with pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET leased_until = now() - interval '1s' WHERE id=%s",
            (run_id,),
        )
    _claim("worker-B", owner)

    assert R.record_result(run_id, _Result("degraded", BUDGET),
                           state="post_pending", worker_id="worker-A") is False
    assert R.mark(run_id, "degraded", worker_id="worker-A",
                  error=BUDGET) is False
    assert R.mark_posted(run_id, state="degraded", posted=True,
                         worker_id="worker-A") is False

    row = R.get_run(run_id)
    assert row["degraded_reason"] is None
    assert row["state"] == "running" and row["worker_id"] == "worker-B"


# -- 6: schema and API compatibility --------------------------------------


def test_the_migration_adds_the_column_to_a_pre_existing_table(db):
    """schema.sql is applied on every boot; it must upgrade an old database."""
    with pool().connection() as conn:
        conn.execute("ALTER TABLE runs DROP COLUMN IF EXISTS degraded_reason")
        assert conn.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_name='runs' AND column_name='degraded_reason'"""
        ).fetchone() is None
    init_db()
    with pool().connection() as conn:
        assert conn.execute(
            """SELECT 1 FROM information_schema.columns
                WHERE table_name='runs' AND column_name='degraded_reason'"""
        ).fetchone() is not None


def test_schema_still_applies_twice_without_error(db):
    init_db()
    init_db()


# -- the migration backfills rows the old code left behind ----------------


def _legacy_row(owner, *, state, error, reason=None):
    """A row as the OLD code would have written it, column dropped and all."""
    run_id = str(uuid.uuid4())
    with pool().connection() as conn:
        conn.execute(
            """INSERT INTO runs (id, pr_url, owner, repo, pr_number, state,
                                 error, degraded_reason, delivery_id)
               VALUES (%s,'u',%s,'r',1,%s,%s,%s,%s)""",
            (run_id, owner, state, error, reason, str(uuid.uuid4())),
        )
    return run_id


def test_a_pre_migration_degraded_row_keeps_its_reason_after_the_migration(db):
    """The deploy window: the reason is in `error`, where the old code left it."""
    owner = f"legacy-{uuid.uuid4().hex[:8]}"
    try:
        with pool().connection() as conn:
            conn.execute("ALTER TABLE runs DROP COLUMN IF EXISTS degraded_reason")
            run_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO runs (id, pr_url, owner, repo, pr_number, state,
                                     error, delivery_id)
                   VALUES (%s,'u',%s,'r',1,'degraded',%s,%s)""",
                (run_id, owner, BUDGET, str(uuid.uuid4())),
            )

        init_db()                                   # the migration

        row = R.get_run(run_id)
        assert row["state"] == "degraded"
        assert row["degraded_reason"] == BUDGET
    finally:
        with pool().connection() as conn:
            conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def test_the_backfill_never_overwrites_a_populated_reason(db):
    owner = f"legacy-{uuid.uuid4().hex[:8]}"
    try:
        run_id = _legacy_row(owner, state="degraded",
                             error="GitHub 429 secondary limit",
                             reason=BUDGET)
        init_db()
        assert R.get_run(run_id)["degraded_reason"] == BUDGET
    finally:
        with pool().connection() as conn:
            conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


@pytest.mark.parametrize("state", ["failed", "published", "queued", "running",
                                   "cancelled", "superseded", "post_pending"])
def test_the_backfill_never_promotes_an_error_on_a_non_degraded_row(db, state):
    """An error is not a degradation reason; copying it would claim the run
    published something partial when it did not."""
    owner = f"legacy-{uuid.uuid4().hex[:8]}"
    try:
        run_id = _legacy_row(owner, state=state, error="PermanentError: 404")
        init_db()
        row = R.get_run(run_id)
        assert row["degraded_reason"] is None
        assert row["error"] == "PermanentError: 404"   # left exactly as it was
    finally:
        with pool().connection() as conn:
            conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def test_the_backfill_is_idempotent_across_repeated_boots(db):
    owner = f"legacy-{uuid.uuid4().hex[:8]}"
    try:
        run_id = _legacy_row(owner, state="degraded", error=BUDGET)
        init_db()
        first = R.get_run(run_id)
        init_db()
        init_db()
        again = R.get_run(run_id)
        assert first["degraded_reason"] == again["degraded_reason"] == BUDGET
    finally:
        with pool().connection() as conn:
            conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def test_a_legacy_degraded_row_with_no_error_is_left_alone(db):
    """Already closed out by the old code: the reason is genuinely gone."""
    owner = f"legacy-{uuid.uuid4().hex[:8]}"
    try:
        run_id = _legacy_row(owner, state="degraded", error=None)
        init_db()
        row = R.get_run(run_id)
        assert row["state"] == "degraded"          # state still honest
        assert row["degraded_reason"] is None      # nothing invented
    finally:
        with pool().connection() as conn:
            conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def test_the_operator_api_reports_the_reason(run):
    from api.schemas import RunSummary

    run_id, owner = run
    _claim(WORKER, owner)
    R.record_result(run_id, _Result("degraded", BUDGET),
                    state="post_pending", worker_id=WORKER)
    R.mark_posted(run_id, state="degraded", posted=True, worker_id=WORKER)

    row = R.get_run(run_id)
    summary = RunSummary(**{k: row.get(k) for k in RunSummary.model_fields})
    assert summary.state == "degraded"
    assert summary.degraded_reason == BUDGET


def test_the_new_field_is_optional_so_old_callers_still_construct(run):
    """Additive: a payload with no degraded_reason must still validate."""
    from api.schemas import RunSummary

    run_id, _ = run
    row = R.get_run(run_id)
    fields = {k: row.get(k) for k in RunSummary.model_fields}
    fields.pop("degraded_reason")
    assert RunSummary(**fields).degraded_reason is None


# -- the worker actually derives the terminal state -----------------------
#
# The assertions above pin the columns. This pins the branch in
# `worker.post_pending` that reads them - no Postgres, because the behaviour
# under test is the worker's, not the SQL's.


class _Store:
    def __init__(self):
        self.calls: list[tuple] = []

    def mark_posted(self, run_id, *, state, posted, **kw):
        self.calls.append(("mark_posted", state, posted))
        return True

    def mark(self, run_id, state, *, error=None, **kw):
        self.calls.append(("mark", state, error))
        return True

    def requeue_post(self, run_id, *, error, delay_s=0.0, **kw):
        self.calls.append(("requeue_post", round(delay_s, 3)))
        return True


PROW = {"id": "r1", "owner": "o", "repo": "r", "pr_number": 1, "attempts": 1,
        "head_sha": "a" * 40, "payload": {"body": "b"}}


@pytest.fixture
def wk(monkeypatch):
    import agent.review as AR
    import worker.main as W

    monkeypatch.setattr(W, "token_provider_for", lambda row: (lambda: "tok"))
    monkeypatch.setattr(AR, "post_payload", lambda *a, **k: True)
    store = _Store()
    monkeypatch.setattr(W, "R", store)
    return W, store


def test_post_pending_publishes_a_normal_review_as_published(wk):
    W, store = wk
    W.post_pending({**PROW, "degraded_reason": None})
    assert ("mark_posted", "published", True) in store.calls


def test_post_pending_publishes_a_degraded_review_as_degraded(wk):
    """It used to hardcode 'published', so a retried post erased the state."""
    W, store = wk
    W.post_pending({**PROW, "degraded_reason": BUDGET})
    assert ("mark_posted", "degraded", True) in store.calls


def test_post_pending_on_a_row_predating_the_column_is_published(wk):
    """A row claimed before the migration has no key at all."""
    W, store = wk
    W.post_pending(dict(PROW))
    assert ("mark_posted", "published", True) in store.calls
