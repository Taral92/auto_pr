"""Ownership fencing: a stale worker cannot mutate a run it no longer owns.

The sequence this closes is real. Worker A stalls long enough for its lease to
lapse - a starved heartbeat thread, a paused container, a CPU-bound tool call -
worker B claims the row and starts reviewing, and then A wakes up. Unfenced, A
extends the lease it no longer holds, overwrites B's result, and posts a second
review.

`claim` writes `worker_id`; every mutation performed under a lease carries that
id into its WHERE clause and returns whether the row matched. These tests run
against REAL Postgres, because the thing under test is the SQL predicate: a
mock would assert only that we passed an argument.
"""

import os
import uuid

import pytest

from config import get_settings
from storage import runs as R
from storage.db import close_pool, init_db, pool

LEASE = 900
MAX_ATTEMPTS = 3
A = "worker-A"
B = "worker-B"


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
        pytest.skip("lease fencing tests need local postgres")
    init_db()
    yield
    close_pool()
    get_settings.cache_clear()


@pytest.fixture
def run(db):
    """One queued run under a unique owner, so parallel runs cannot collide."""
    owner = f"fence-{uuid.uuid4().hex[:8]}"
    run_id = R.insert_queued(
        pr_url="https://github.com/o/r/pull/1", owner=owner, repo="r",
        pr_number=1, head_sha="a" * 40, delivery_id=str(uuid.uuid4()),
    )
    yield run_id, owner
    with pool().connection() as conn:
        conn.execute("DELETE FROM runs WHERE owner=%s", (owner,))


def _claim_as(worker_id, owner):
    """Claim repeatedly until we get this fixture's own row."""
    for _ in range(20):
        row = R.claim(lease_s=LEASE, worker_id=worker_id,
                      max_attempts=MAX_ATTEMPTS)
        if row is None:
            return None
        if row["owner"] == owner:
            return row
    return None


def _expire(run_id):
    """Age the lease out, exactly as a stalled worker would."""
    with pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET leased_until = now() - interval '1 second' WHERE id=%s",
            (run_id,),
        )


class _Result:
    """The shape record_result reads off a ReviewResult."""
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
    error = None


@pytest.fixture
def handover(run):
    """A claims, A's lease expires, B claims. B is now the owner."""
    run_id, owner = run
    first = _claim_as(A, owner)
    assert first is not None and first["worker_id"] == A
    _expire(run_id)
    second = _claim_as(B, owner)
    assert second is not None and second["worker_id"] == B
    return run_id, owner


# -- 1 + 2: claim establishes and transfers ownership ---------------------


def test_claim_sets_the_worker_id(run):
    run_id, owner = run
    row = _claim_as(A, owner)

    assert row["worker_id"] == A
    assert row["state"] == "running"
    assert row["prior_state"] == "queued"
    assert R.get_run(run_id)["worker_id"] == A


def test_an_expired_lease_transfers_ownership_to_the_next_claimer(handover):
    run_id, _ = handover
    assert R.get_run(run_id)["worker_id"] == B


def test_a_live_lease_is_not_claimable_by_another_worker(run):
    """The fence is the second line; SKIP LOCKED + the lease is the first."""
    run_id, owner = run
    _claim_as(A, owner)
    assert _claim_as(B, owner) is None


# -- 3-6: the stale worker is refused -------------------------------------


def test_a_stale_worker_cannot_extend_the_lease(handover):
    run_id, _ = handover
    before = R.get_run(run_id)["leased_until"]

    assert R.heartbeat(run_id, lease_s=LEASE * 4, worker_id=A) is False
    assert R.get_run(run_id)["leased_until"] == before


def test_a_stale_worker_cannot_record_a_result(handover):
    run_id, _ = handover

    assert R.record_result(run_id, _Result(), state="post_pending",
                           worker_id=A) is False
    row = R.get_run(run_id)
    assert row["state"] == "running"          # still B's, still in flight
    assert row["payload"] is None
    assert row["head_sha"] == "a" * 40        # not overwritten with _Result's


def test_a_stale_worker_cannot_requeue_the_run(handover):
    run_id, _ = handover

    assert R.requeue(run_id, error="stale", worker_id=A) is False
    row = R.get_run(run_id)
    assert row["state"] == "running" and row["worker_id"] == B
    assert row["error"] is None


def test_a_stale_worker_cannot_requeue_the_post(handover):
    run_id, _ = handover

    assert R.requeue_post(run_id, error="stale", worker_id=A) is False
    assert R.get_run(run_id)["state"] == "running"


def test_a_stale_worker_cannot_mark_the_run_posted(handover):
    run_id, _ = handover

    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=A) is False
    assert R.get_run(run_id)["state"] == "running"


def test_a_stale_worker_cannot_mark_the_run_terminal(handover):
    run_id, _ = handover

    assert R.mark(run_id, "failed", worker_id=A, error="stale") is False
    assert R.get_run(run_id)["state"] == "running"


def test_a_stale_worker_cannot_flag_another_workers_findings_as_posted(handover):
    """mark_posted's second statement must be gated on the first."""
    run_id, _ = handover
    R.record_result(run_id, _Result(), state="post_pending", worker_id=B)
    with pool().connection() as conn:
        conn.execute(
            """INSERT INTO findings (id, run_id, severity, category, path,
                   title, body, evidence, verdict, anchored, posted)
               VALUES (%s,%s,'nit','correctness','a.py','t','b','e',
                       'grounded','inline',FALSE)""",
            (str(uuid.uuid4()), run_id),
        )

    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=A) is False
    assert all(not f["posted"] for f in R.findings_for(run_id))


# -- 7: the real owner can still do everything ----------------------------


def test_the_new_owner_can_heartbeat(handover):
    run_id, _ = handover
    before = R.get_run(run_id)["leased_until"]

    assert R.heartbeat(run_id, lease_s=LEASE * 4, worker_id=B) is True
    assert R.get_run(run_id)["leased_until"] > before


def test_the_new_owner_can_record_a_result_and_mark_it_posted(handover):
    run_id, _ = handover

    assert R.record_result(run_id, _Result(), state="post_pending",
                           worker_id=B) is True
    assert R.get_run(run_id)["payload"] == {"body": "b"}
    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=B) is True
    assert R.get_run(run_id)["state"] == "published"


def test_the_new_owner_can_requeue(handover):
    run_id, _ = handover

    assert R.requeue(run_id, error="429", worker_id=B, delay_s=30) is True
    row = R.get_run(run_id)
    assert row["state"] == "queued"
    assert row["worker_id"] is None          # ownership released
    assert row["not_before"] is not None


def test_requeue_releases_ownership_so_even_the_owner_is_then_fenced(run):
    """After handing the run back, B's own later writes must not apply."""
    run_id, owner = run
    _claim_as(B, owner)
    assert R.requeue(run_id, error="429", worker_id=B) is True

    assert R.mark(run_id, "failed", worker_id=B) is False
    assert R.heartbeat(run_id, lease_s=LEASE, worker_id=B) is False
    assert R.get_run(run_id)["state"] == "queued"


# -- 9: post_pending is leased too ----------------------------------------


def test_a_post_pending_row_is_claimed_with_a_worker_id(run):
    """It is NOT an unowned operation: claim leases it like any other row."""
    run_id, owner = run
    _claim_as(A, owner)
    R.record_result(run_id, _Result(), state="post_pending", worker_id=A)
    R.requeue_post(run_id, error="429", worker_id=A, delay_s=0)
    assert R.get_run(run_id)["worker_id"] is None      # released, claimable

    row = _claim_as(B, owner)
    assert row["prior_state"] == "post_pending"
    assert row["worker_id"] == B and row["state"] == "running"


def test_the_post_pending_retry_is_fenced_to_its_claimer(run):
    run_id, owner = run
    _claim_as(A, owner)
    R.record_result(run_id, _Result(), state="post_pending", worker_id=A)
    R.requeue_post(run_id, error="429", worker_id=A, delay_s=0)
    _claim_as(B, owner)                                # B now owns the post

    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=A) is False
    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=B) is True
    assert R.get_run(run_id)["state"] == "published"
    # The payload survived the handover - C2 is untouched.
    assert R.get_run(run_id)["payload"] == {"body": "b"}


# -- 8: single-worker behaviour is unchanged ------------------------------


def test_the_ordinary_single_worker_lifecycle_still_works(run):
    run_id, owner = run
    row = _claim_as(A, owner)
    assert row["prior_state"] == "queued"

    assert R.heartbeat(run_id, lease_s=LEASE, worker_id=A) is True
    assert R.record_result(run_id, _Result(), state="post_pending",
                           worker_id=A) is True
    assert R.mark_posted(run_id, state="published", posted=True,
                         worker_id=A) is True

    row = R.get_run(run_id)
    assert row["state"] == "published" and row["error"] is None


def test_a_worker_reclaiming_its_own_abandoned_run_is_not_fenced_out(run):
    """Same worker_id after a crash-and-reclaim: ownership is genuinely ours."""
    run_id, owner = run
    _claim_as(A, owner)
    _expire(run_id)
    again = _claim_as(A, owner)

    assert again["worker_id"] == A
    assert R.heartbeat(run_id, lease_s=LEASE, worker_id=A) is True


# -- unfenced operations stay unfenced ------------------------------------


def test_coalesce_still_cancels_a_run_another_worker_owns(run):
    """The webhook must be able to cancel in-flight work it does not own."""
    run_id, owner = run
    _claim_as(A, owner)

    out = R.coalesce_pr(owner, "r", 1, "b" * 40)

    assert out["cancelled"] == 1
    assert R.get_run(run_id)["cancel"] is True
    assert R.get_run(run_id)["worker_id"] == A      # ownership untouched


def test_the_operator_can_still_cancel_a_run_it_does_not_own(run):
    run_id, owner = run
    _claim_as(A, owner)

    assert R.set_cancel(run_id) is True
    assert R.is_cancelled(run_id) is True


def test_the_reaper_still_closes_out_a_row_whose_worker_is_gone(run):
    run_id, owner = run
    for _ in range(MAX_ATTEMPTS):
        _claim_as(A, owner)
        _expire(run_id)

    assert run_id in R.reap_exhausted(max_attempts=MAX_ATTEMPTS)
    row = R.get_run(run_id)
    assert row["state"] == "failed" and row["worker_id"] is None


# -- what the WORKER does when a fenced write is refused ------------------
#
# The SQL above proves a stale write cannot land. These prove the worker acts
# on that answer instead of carrying on as though it had succeeded - which is
# the half that actually prevents the second review from being posted. No
# Postgres: the storage layer is the fake here, because the behaviour under
# test is the branching in worker/main.py.


class _LosingStore:
    """storage.runs, where the fenced calls report we no longer own the row."""

    def __init__(self, lose=(), cancelled=False):
        self.lose = set(lose)
        self.calls: list[tuple] = []
        self._cancelled = cancelled

    def _result(self, name, *detail):
        self.calls.append((name, *detail))
        return name not in self.lose

    def record_result(self, run_id, result, *, state, **kw):
        return self._result("record_result", state)

    def mark_posted(self, run_id, *, state, posted, **kw):
        return self._result("mark_posted", state, posted)

    def requeue_post(self, run_id, *, error, delay_s=0.0, **kw):
        return self._result("requeue_post", round(delay_s, 3))

    def requeue(self, run_id, *, error, delay_s=0.0, **kw):
        return self._result("requeue", round(delay_s, 3))

    def mark(self, run_id, state, *, error=None, **kw):
        return self._result("mark", state)

    def heartbeat(self, run_id, *, lease_s, worker_id):
        return self._result("heartbeat")

    def is_cancelled(self, run_id):
        return self._cancelled


class _FakeResult:
    status = "published"
    dry_run = False
    head_sha = "a" * 40
    payload = {"body": "b"}
    grounding = {"grounded": 1}
    anchoring = {"inline": 1}


WROW = {"id": "r1", "owner": "o", "repo": "r", "pr_number": 1, "attempts": 1,
        "dry_run": False, "head_sha": "a" * 40}


@pytest.fixture
def wk(monkeypatch):
    import agent.review as AR
    import worker.main as W

    monkeypatch.setattr(W, "token_provider_for", lambda row: (lambda: "tok"))
    posts = []
    monkeypatch.setattr(AR, "review_pr", lambda *a, **k: _FakeResult())
    monkeypatch.setattr(AR, "post_payload",
                        lambda *a, **k: (posts.append(a), True)[1])
    return W, posts, monkeypatch


def test_losing_the_lease_at_the_commit_point_stops_the_post(wk):
    """The whole point. A stale result must not become a second review."""
    W, posts, monkeypatch = wk
    store = _LosingStore(lose={"record_result"})
    monkeypatch.setattr(W, "R", store)

    W.run_one(dict(WROW))

    assert posts == []                                   # nothing reached GitHub
    assert [c[0] for c in store.calls] == ["record_result"]   # and it stopped there


def test_keeping_the_lease_posts_as_before(wk):
    W, posts, monkeypatch = wk
    monkeypatch.setattr(W, "R", _LosingStore())

    W.run_one(dict(WROW))

    assert len(posts) == 1


def test_a_refused_mark_posted_does_not_read_as_success(wk, capsys):
    W, posts, monkeypatch = wk
    monkeypatch.setattr(W, "R", _LosingStore(lose={"mark_posted"}))

    W.run_one(dict(WROW))
    out = capsys.readouterr().out

    assert "lease lost to another worker" in out
    assert "posted=True" not in out


def test_a_refused_requeue_does_not_claim_a_retry_was_scheduled(wk, capsys):
    from core.errors import TransientError

    import agent.review as AR

    W, _, monkeypatch = wk
    monkeypatch.setattr(
        AR, "review_pr",
        lambda *a, **k: (_ for _ in ()).throw(TransientError("blip")),
    )
    monkeypatch.setattr(W, "R", _LosingStore(lose={"requeue"}))

    W.run_one(dict(WROW))
    out = capsys.readouterr().out

    assert "lease lost to another worker" in out
    assert "transient, retry in" not in out


def test_a_lost_heartbeat_cancels_the_run_through_the_graphs_own_hook(wk):
    """The run must unwind at the next node boundary, not work on for minutes."""
    from agent.runtime import is_cancelled

    W, _, monkeypatch = wk
    monkeypatch.setattr(W, "R", _LosingStore(lose={"heartbeat"}))
    monkeypatch.setattr(W.get_settings(), "lease_s", 0.03, raising=False)

    seen = {}

    def slow_review(*a, **k):
        import time
        for _ in range(100):
            time.sleep(0.01)
            if is_cancelled.get()():
                seen["cancelled_at"] = True
                break
        return _FakeResult()

    import agent.review as AR
    monkeypatch.setattr(AR, "review_pr", slow_review)
    W.run_one(dict(WROW))

    assert seen.get("cancelled_at") is True


def test_a_lost_lease_is_not_recorded_as_a_cancellation(wk, capsys):
    """`Cancelled` from a lost lease must not write a terminal state."""
    from core.errors import Cancelled

    import agent.review as AR

    W, _, monkeypatch = wk
    store = _LosingStore(lose={"heartbeat"})
    monkeypatch.setattr(W, "R", store)
    monkeypatch.setattr(W.get_settings(), "lease_s", 0.03, raising=False)

    def review(*a, **k):
        import time
        time.sleep(0.2)                       # long enough to lose the lease
        raise Cancelled("run cancelled")

    monkeypatch.setattr(AR, "review_pr", review)
    W.run_one(dict(WROW))

    assert not any(c[0] == "mark" for c in store.calls)
    assert "released to its new owner" in capsys.readouterr().out


def test_a_genuine_cancellation_is_still_marked(wk):
    from core.errors import Cancelled

    import agent.review as AR

    W, _, monkeypatch = wk
    store = _LosingStore(cancelled=True)
    monkeypatch.setattr(W, "R", store)
    monkeypatch.setattr(
        AR, "review_pr",
        lambda *a, **k: (_ for _ in ()).throw(Cancelled("run cancelled")),
    )

    W.run_one(dict(WROW))

    assert ("mark", "cancelled") in store.calls


def test_post_pending_stops_when_its_lease_is_gone(wk, capsys):
    W, posts, monkeypatch = wk
    store = _LosingStore(lose={"mark_posted"})
    monkeypatch.setattr(W, "R", store)

    W.post_pending({**WROW, "payload": {"body": "b"}})

    assert len(posts) == 1                       # the post itself still happened
    out = capsys.readouterr().out
    assert "lease lost to another worker" in out
    assert "no model work" not in out            # but it did not report success
