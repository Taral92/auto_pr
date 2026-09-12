"""The oversized-diff branch: persist before post, and post exactly once.

This branch used to be the one place in the pipeline that ignored `post=False`.
It POSTed to GitHub the moment the diff came back over the cap, then returned
`published`, so `worker.run_one` committed `post_pending` and called
`post_payload` on the same payload - a second, identical review. Nothing caught
it, because `too_large_payload` carried no marker and `already_reviewed`
therefore could not recognise the review it had just posted.

Both halves are tested here: the payload carries the marker, and the branch
honours `post` exactly like the normal path.
"""

import json

import pytest

import agent.review as AR
from gh.anchor import TOO_LARGE, too_large_payload
from gh.client import MARKER

TOKEN = "ghs_SECRET_TOKEN_VALUE"
SHA = "a" * 40
BIG = "diff --git a/x b/x\n" + ("+padding\n" * 50_000)
SMALL = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n"
    "+++ b/x.py\n"
    "@@ -1,1 +1,1 @@\n"
    "-old\n"
    "+new\n"
)


@pytest.fixture
def seam(monkeypatch, tmp_path):
    """Fake GitHub at `agent.review`'s own seam. No network, no model.

    `post_review` and `already_reviewed` are the two names `post_payload`
    resolves out of this module, so patching them here exercises the real
    `post_payload` - including its idempotency check - while counting posts.
    """
    calls = {"posts": [], "reviewed": False, "diff": BIG}

    monkeypatch.setattr(AR, "get_pr", lambda *a: {"head": {"sha": SHA}})
    monkeypatch.setattr(AR, "get_diff", lambda *a: calls["diff"])
    monkeypatch.setattr(AR, "already_reviewed", lambda *a: calls["reviewed"])
    monkeypatch.setattr(
        AR, "post_review", lambda o, r, n, t, payload: calls["posts"].append(payload)
    )
    # Keep the trace file out of the repo; the write itself stays real.
    monkeypatch.setattr(AR, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(AR.get_settings(), "max_diff_bytes", 1024, raising=False)
    return calls


# -- the payload carries the marker ---------------------------------------


def test_the_oversized_payload_carries_the_idempotency_marker():
    key = AR.idempotency_key("o", "r", 1, SHA)
    payload = too_large_payload(SHA, key)
    assert MARKER.format(key=key) in payload["body"]


def test_the_oversized_payload_keeps_its_existing_content():
    """Only the marker is added. The notice, event and commit are unchanged."""
    payload = too_large_payload(SHA, "k" * 16)
    assert payload["commit_id"] == SHA
    assert payload["event"] == "COMMENT"
    assert payload["comments"] == []
    assert payload["body"].startswith(TOO_LARGE)


def test_an_unmarked_oversized_payload_can_no_longer_be_built():
    """The key is mandatory: the bug was a payload without one."""
    with pytest.raises(TypeError):
        too_large_payload(SHA)


def test_already_reviewed_recognises_the_marker_this_payload_carries(monkeypatch):
    """The two halves actually meet - the real checker finds the real marker."""
    import gh.client as C

    key = AR.idempotency_key("o", "r", 1, SHA)
    body = too_large_payload(SHA, key)["body"]
    monkeypatch.setattr(
        C, "_request", lambda *a, **k: (json.dumps([{"body": body}]), 200)
    )
    assert C.already_reviewed("o", "r", 1, TOKEN, key) is True
    assert C.already_reviewed("o", "r", 1, TOKEN, "different_key") is False


# -- post=False computes but never posts ----------------------------------


def test_post_false_never_posts_an_oversized_review(seam):
    """The regression. The worker runs the pipeline with post=False so that
    NOTHING reaches GitHub before record_result commits."""
    result = AR.review_pr("o", "r", 1, TOKEN, post=False)

    assert seam["posts"] == []
    assert result.posted is False
    assert result.payload["body"].startswith(TOO_LARGE)
    assert MARKER.format(key=AR.idempotency_key("o", "r", 1, SHA)) in result.payload["body"]


def test_a_dry_run_never_posts_an_oversized_review(seam):
    assert AR.review_pr("o", "r", 1, TOKEN, dry_run=True).posted is False
    assert seam["posts"] == []


def test_post_true_still_posts_once_for_the_cli(seam):
    """The CLI default is unchanged - it just goes through post_payload now."""
    result = AR.review_pr("o", "r", 1, TOKEN, post=True)
    assert len(seam["posts"]) == 1
    assert result.posted is True


def test_post_true_skips_when_the_marker_is_already_on_the_pr(seam):
    seam["reviewed"] = True
    result = AR.review_pr("o", "r", 1, TOKEN, post=True)
    assert seam["posts"] == []
    assert result.posted is False


# -- normal-sized reviews are untouched -----------------------------------


def test_a_normal_sized_diff_does_not_take_this_branch(seam, monkeypatch):
    """Under the cap the run goes to the clone and the graph, as before."""
    seam["diff"] = SMALL
    monkeypatch.setattr(
        AR, "clone_head",
        lambda *a: (_ for _ in ()).throw(RuntimeError("reached the clone")),
    )
    with pytest.raises(RuntimeError, match="reached the clone"):
        AR.review_pr("o", "r", 1, TOKEN, post=False)
    assert seam["posts"] == []


# -- the worker's persist-then-post lifecycle -----------------------------


class FakeStore:
    """storage.runs, recording the calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.rows: dict = {}

    def record_result(self, run_id, result, *, state):
        self.calls.append(("record_result", state))
        self.rows[run_id] = {"payload": result.payload, "state": state}

    def mark_posted(self, run_id, *, state, posted):
        self.calls.append(("mark_posted", state, posted))

    def requeue_post(self, run_id, *, error, delay_s=0.0):
        self.calls.append(("requeue_post", round(delay_s, 3)))

    def requeue(self, run_id, *, error, delay_s=0.0):
        self.calls.append(("requeue", round(delay_s, 3)))

    def mark(self, run_id, state, *, error=None):
        self.calls.append(("mark", state, error))

    def heartbeat(self, *a, **k):
        pass

    def is_cancelled(self, *a, **k):
        return False


ROW = {"id": "r1", "owner": "o", "repo": "r", "pr_number": 1, "attempts": 1,
       "dry_run": False, "head_sha": SHA}


@pytest.fixture
def worker(monkeypatch, seam):
    import worker.main as W

    store = FakeStore()
    monkeypatch.setattr(W, "R", store)
    monkeypatch.setattr(W, "token_provider_for", lambda row: (lambda: TOKEN))
    return W, store, seam


def test_an_oversized_pr_is_persisted_before_it_is_posted(worker):
    W, store, seam = worker
    W.run_one(dict(ROW))

    kinds = [c[0] for c in store.calls]
    assert kinds.index("record_result") < kinds.index("mark_posted")
    assert ("record_result", "post_pending") in store.calls


def test_an_oversized_pr_is_posted_exactly_once(worker):
    """The bug: review_pr posted, then the worker posted the same notice again."""
    W, store, seam = worker
    W.run_one(dict(ROW))

    assert len(seam["posts"]) == 1
    assert seam["posts"][0]["body"].startswith(TOO_LARGE)
    assert ("mark_posted", "published", True) in store.calls


def test_the_persisted_oversized_payload_carries_the_marker(worker):
    W, store, seam = worker
    W.run_one(dict(ROW))

    body = store.rows["r1"]["payload"]["body"]
    assert MARKER.format(key=AR.idempotency_key("o", "r", 1, SHA)) in body


def test_a_retried_oversized_post_is_idempotent(worker):
    """The review is already on the PR; the retry must be a no-op."""
    W, store, seam = worker
    W.run_one(dict(ROW))
    assert len(seam["posts"]) == 1

    # The marker is now visible on the PR, exactly as after a real first post.
    seam["reviewed"] = True
    W.post_pending({**ROW, "payload": store.rows["r1"]["payload"]})

    assert len(seam["posts"]) == 1                        # still one
    assert ("mark_posted", "published", False) in store.calls


def test_an_oversized_post_that_fails_retries_the_post_not_the_review(worker,
                                                                     monkeypatch):
    """A post failure must not re-run review_pr - the C2 invariant, on this
    branch too, which was unreachable while the branch posted for itself."""
    from core.errors import TransientError

    W, store, seam = worker
    monkeypatch.setattr(
        AR, "post_review",
        lambda *a: (_ for _ in ()).throw(
            TransientError("429", code=429, retry_after=30)),
    )
    W.run_one(dict(ROW))

    assert ("record_result", "post_pending") in store.calls   # review kept
    assert ("requeue_post", 30.0) in store.calls              # post-only retry
    assert not any(c[0] == "requeue" for c in store.calls)
