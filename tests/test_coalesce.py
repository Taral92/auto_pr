"""head_sha must be stored at enqueue or coalesce treats every in-flight
run as stale. Needs local Postgres (CI service or compose db). Skipped
against a remote DATABASE_URL so pytest cannot write to the live database.
"""

import os
import uuid

import pytest

from config import get_settings
from storage import runs as R
from storage.db import close_pool, init_db, pool


SHA_A = "a" * 40
SHA_B = "b" * 40


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
        pytest.skip("coalesce tests need local postgres")
    init_db()
    yield
    close_pool()
    get_settings.cache_clear()


@pytest.fixture
def pr(db):
    owner = f"coalesce-{uuid.uuid4().hex[:8]}"
    yield {
        "pr_url": "https://github.com/o/r/pull/1",
        "owner": owner,
        "repo": "r",
        "pr_number": 1,
    }
    with pool().connection() as conn:
        conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def _enqueue(pr, sha, delivery=None):
    return R.insert_queued(
        **pr, head_sha=sha, delivery_id=delivery or str(uuid.uuid4())
    )


def _mark_running(run_id):
    with pool().connection() as conn:
        conn.execute("UPDATE runs SET state='running' WHERE id=%s", (run_id,))


def test_insert_stores_head_sha(pr):
    run_id = _enqueue(pr, SHA_A)
    assert R.get_run(run_id)["head_sha"] == SHA_A


def test_same_sha_leaves_queued_alone(pr):
    run_id = _enqueue(pr, SHA_A)
    out = R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_A)
    assert out == {"superseded": 0, "cancelled": 0}
    assert R.get_run(run_id)["state"] == "queued"


def test_new_sha_supersedes_queued(pr):
    run_id = _enqueue(pr, SHA_A)
    out = R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_B)
    assert out["superseded"] == 1
    assert R.get_run(run_id)["state"] == "superseded"


def test_same_sha_does_not_cancel_running(pr):
    run_id = _enqueue(pr, SHA_A)
    _mark_running(run_id)
    out = R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_A)
    assert out == {"superseded": 0, "cancelled": 0}
    assert not R.is_cancelled(run_id)


def test_new_sha_cancels_running(pr):
    run_id = _enqueue(pr, SHA_A)
    _mark_running(run_id)
    out = R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_B)
    assert out["cancelled"] == 1
    assert R.is_cancelled(run_id)
    assert R.get_run(run_id)["state"] == "running"


# --- the race: coalesce and insert must be one atomic step ----------------
#
# `coalesce_pr` + `insert_queued` as two transactions is only correct while a
# single process serves webhooks. These pin `enqueue_coalesced`, which is the
# one the webhook actually calls.

def _queued(owner):
    with pool().connection() as conn:
        return conn.execute(
            "SELECT id, head_sha FROM runs WHERE owner=%s AND state='queued'",
            (owner,),
        ).fetchall()


def test_two_transaction_coalesce_loses_the_race(pr):
    """The interleaving the old webhook ordering allowed. Documents WHY."""
    R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_A)
    R.coalesce_pr(pr["owner"], pr["repo"], pr["pr_number"], SHA_B)
    _enqueue(pr, SHA_A)
    _enqueue(pr, SHA_B)
    # Both survive: B coalesced before A had landed, so it found nothing.
    assert len(_queued(pr["owner"])) == 2


def test_enqueue_coalesced_retires_the_older_head(pr):
    a, _ = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=str(uuid.uuid4()))
    b, out = R.enqueue_coalesced(**pr, head_sha=SHA_B, delivery_id=str(uuid.uuid4()))
    assert out["superseded"] == 1
    assert R.get_run(a)["state"] == "superseded"
    assert [r["id"] for r in _queued(pr["owner"])] == [b]


def test_enqueue_coalesced_keeps_the_same_head(pr):
    a, _ = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=str(uuid.uuid4()))
    b, out = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=str(uuid.uuid4()))
    assert out == {"superseded": 0, "cancelled": 0}
    assert {r["id"] for r in _queued(pr["owner"])} == {a, b}


def test_enqueue_coalesced_cancels_a_running_older_head(pr):
    a, _ = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=str(uuid.uuid4()))
    _mark_running(a)
    _, out = R.enqueue_coalesced(**pr, head_sha=SHA_B, delivery_id=str(uuid.uuid4()))
    assert out["cancelled"] == 1
    assert R.is_cancelled(a)


def test_redelivery_is_rejected_and_retires_nothing(pr):
    """A replayed delivery must not supersede the work it first scheduled."""
    delivery = str(uuid.uuid4())
    a, _ = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=delivery)
    again, out = R.enqueue_coalesced(**pr, head_sha=SHA_A, delivery_id=delivery)
    assert again is None
    assert out == {"superseded": 0, "cancelled": 0}
    assert [r["id"] for r in _queued(pr["owner"])] == [a]
    assert R.get_run(a)["state"] == "queued"


def test_a_second_delivery_waits_for_the_first(pr):
    """Serialization, proved deterministically rather than by racing.

    An 8-thread free-for-all is not evidence: the window between the insert
    and the retirement is narrow, and the test passes with the lock REMOVED
    whenever the threads happen not to interleave inside it. So this holds the
    PR's advisory lock open by hand and asserts the enqueue cannot proceed
    until it is released - which fails immediately if the lock is dropped.
    """
    import threading

    key = R._pr_lock_key(pr["owner"], pr["repo"], pr["pr_number"])
    done = threading.Event()
    errors: list[BaseException] = []

    def push():
        try:
            R.enqueue_coalesced(
                **pr, head_sha=SHA_B, delivery_id=str(uuid.uuid4())
            )
            done.set()
        except BaseException as e:
            errors.append(e)
            done.set()

    # `conn.transaction()` for the same reason `enqueue_coalesced` needs one:
    # the pool is autocommit, so an xact lock without it is released instantly.
    with pool().connection() as holder, holder.transaction():
        holder.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        t = threading.Thread(target=push)
        t.start()
        # Still blocked: the holder's transaction is open, so nothing landed.
        assert not done.wait(timeout=2.0), "enqueue did not wait for the lock"
        assert _queued(pr["owner"]) == []
    # Holder's transaction committed, lock released - the waiter proceeds.
    assert done.wait(timeout=10), "enqueue never completed after release"
    t.join(timeout=10)
    assert not errors, errors
    assert len(_queued(pr["owner"])) == 1


def test_concurrent_pushes_do_not_error_or_deadlock(pr):
    """Eight at once. Not a race test - a test that the lock is not a trap.

    Every delivery for one PR now queues behind the same lock, so this pins
    that contention resolves instead of deadlocking or exhausting the pool,
    and that exactly one row survives.
    """
    import threading

    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def push(i):
        try:
            barrier.wait(timeout=10)
            R.enqueue_coalesced(
                **pr, head_sha=f"{i:040x}", delivery_id=str(uuid.uuid4())
            )
        except BaseException as e:   # a thread that dies must fail the test
            errors.append(e)

    threads = [threading.Thread(target=push, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(_queued(pr["owner"])) == 1
