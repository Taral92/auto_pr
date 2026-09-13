"""Retry, backoff, and the review/post split.

The failure this closes: the GitHub post lived inside the same unit of work as
the model pipeline, and persistence happened after it. A transient 429 at post
time threw away a finished, paid-for review and re-ran every model turn - up to
three times, about two seconds apart, asking a rate limiter the same question.

No network and no model: the GitHub and OpenAI layers are faked at their own
seams, and the lifecycle test drives the worker's real functions against a fake
storage module.
"""

import random
import urllib.error

import pytest

from core.backoff import BASE_DELAY_S, JITTER, MAX_DELAY_S, delay_for
from core.errors import PermanentError, TransientError
from gh.client import retry_hint

TOKEN = "ghs_SECRET_TOKEN_VALUE"


# -- backoff ---------------------------------------------------------------


def test_a_service_supplied_delay_is_honoured_exactly():
    """No jitter on an explicit hint: smearing it pushes some workers back
    inside the window the service just told us to stay out of."""
    assert delay_for(1, retry_after=42) == 42.0
    assert delay_for(5, retry_after=7.5) == 7.5


def test_a_service_supplied_delay_is_still_capped():
    assert delay_for(1, retry_after=10**9) == MAX_DELAY_S


def test_backoff_grows_and_is_bounded():
    rand = random.Random(0)
    waits = [delay_for(a, rand=rand) for a in range(1, 12)]
    assert waits[0] < waits[1] < waits[2]
    assert all(0.0 <= w <= MAX_DELAY_S for w in waits)
    assert max(waits) == pytest.approx(MAX_DELAY_S, rel=JITTER + 0.01)


def test_backoff_never_negative_or_over_cap_over_many_draws():
    rand = random.Random(11)
    draws = [delay_for(a, rand=rand) for a in range(1, 30) for _ in range(40)]
    assert min(draws) >= 0.0
    assert max(draws) <= MAX_DELAY_S


def test_backoff_has_jitter_so_workers_do_not_return_in_lockstep():
    a = [delay_for(3, rand=random.Random(s)) for s in range(25)]
    assert len(set(a)) > 1


def test_the_first_retry_is_not_immediate():
    """An immediate requeue is what turned three attempts into six seconds."""
    assert delay_for(1, rand=random.Random(3)) >= BASE_DELAY_S * (1 - JITTER)


# -- GitHub rate-limit detection ------------------------------------------


def test_retry_after_seconds_is_read():
    assert retry_hint({"Retry-After": "60"}) == 60.0


def test_retry_after_http_date_is_read():
    from email.utils import parsedate_to_datetime

    when = "Wed, 21 Oct 2026 07:28:00 GMT"
    epoch = parsedate_to_datetime(when).timestamp()
    assert retry_hint({"Retry-After": when}, now=epoch - 90) == pytest.approx(90, abs=1)


def test_a_past_retry_after_date_is_zero_not_negative():
    assert retry_hint({"Retry-After": "Wed, 21 Oct 2020 07:28:00 GMT"}) == 0.0


def test_x_ratelimit_reset_is_the_fallback():
    hint = retry_hint(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000120"}, now=1000000.0
    )
    assert hint == 120.0


def test_ratelimit_reset_is_ignored_while_quota_remains():
    assert retry_hint(
        {"X-RateLimit-Remaining": "57", "X-RateLimit-Reset": "1000120"}, now=1000000.0
    ) is None


def test_retry_after_wins_over_ratelimit_reset():
    hint = retry_hint(
        {"Retry-After": "5", "X-RateLimit-Remaining": "0",
         "X-RateLimit-Reset": "1000999"}, now=1000000.0
    )
    assert hint == 5.0


@pytest.mark.parametrize("headers", [
    {}, None, {"Retry-After": "soon"}, {"Retry-After": ""},
    {"X-RateLimit-Remaining": "0"}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "x"},
])
def test_an_unreadable_hint_is_absent_not_an_exception(headers):
    """Failing to read a hint must never turn a retryable error into a crash."""
    assert retry_hint(headers) is None


def _http_error(code, headers):
    return urllib.error.HTTPError("u", code, "m", headers, None)


def test_a_429_becomes_transient_carrying_the_hint(monkeypatch):
    from gh import client as C

    def boom(*a, **k):
        raise _http_error(429, {"Retry-After": "33"})

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    with pytest.raises(TransientError) as e:
        C.get_pr("o", "r", 1, TOKEN)
    assert e.value.code == 429
    assert e.value.retry_after == 33.0


def test_a_403_with_ratelimit_headers_is_transient_not_permanent(monkeypatch):
    """GitHub uses 403 for secondary limits; dead-lettering those drops reviews
    for a condition that clears on its own."""
    from gh import client as C

    def boom(*a, **k):
        raise _http_error(403, {"X-RateLimit-Remaining": "0",
                                "X-RateLimit-Reset": "99999999999"})

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    with pytest.raises(TransientError):
        C.get_pr("o", "r", 1, TOKEN)


def test_a_plain_403_is_still_permanent(monkeypatch):
    from gh import client as C

    def boom(*a, **k):
        raise _http_error(403, {})

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    with pytest.raises(PermanentError):
        C.get_pr("o", "r", 1, TOKEN)


def test_a_404_is_still_permanent(monkeypatch):
    from gh import client as C

    def boom(*a, **k):
        raise _http_error(404, {})

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    with pytest.raises(PermanentError):
        C.get_pr("o", "r", 1, TOKEN)


def test_a_github_error_never_echoes_the_token(monkeypatch):
    from gh import client as C

    def boom(*a, **k):
        raise _http_error(500, {})

    monkeypatch.setattr(C.urllib.request, "urlopen", boom)
    with pytest.raises(TransientError) as e:
        C.get_pr("o", "r", 1, TOKEN)
    assert TOKEN not in str(e.value)


# -- OpenAI transient handling --------------------------------------------


class _Resp:
    def __init__(self, headers):
        self.headers = headers


def _status_error(code, headers=None):
    from openai import APIStatusError

    err = APIStatusError.__new__(APIStatusError)
    Exception.__init__(err, f"status {code}")
    err.status_code = code
    err.response = _Resp(headers or {})
    return err


def _client(monkeypatch, responses):
    """A ModelClient whose OpenAI call yields `responses` in order."""
    from agent.model_client import ModelClient

    calls = {"n": 0}

    class _Responses:
        def create(self, **kw):
            i = calls["n"]
            calls["n"] += 1
            item = responses[min(i, len(responses) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

    class _OpenAI:
        def __init__(self, **kw):
            self.responses = _Responses()

    import openai

    monkeypatch.setattr(openai, "OpenAI", _OpenAI)
    monkeypatch.setattr("agent.model_client.time.sleep", lambda *_: None)
    return ModelClient(mode="live"), calls


class _Ok:
    status = "completed"
    incomplete_details = None
    output: list = []

    class usage:
        input_tokens = 5
        output_tokens = 1
        input_tokens_details = None
        output_tokens_details = None


def test_a_transient_openai_failure_is_retried_in_process(monkeypatch):
    """A 429 mid-review must not abandon the turns already paid for."""
    client, calls = _client(monkeypatch, [_status_error(429), _Ok()])
    client.call(system="s", messages=[], tools=None)
    assert calls["n"] == 2


def test_a_permanent_openai_failure_is_not_retried(monkeypatch):
    client, calls = _client(monkeypatch, [_status_error(400)])
    with pytest.raises(PermanentError):
        client.call(system="s", messages=[], tools=None)
    assert calls["n"] == 1


def test_openai_retries_are_bounded(monkeypatch):
    from agent.model_client import MAX_CALL_RETRIES

    client, calls = _client(monkeypatch, [_status_error(503)])
    with pytest.raises(TransientError):
        client.call(system="s", messages=[], tools=None)
    assert calls["n"] == MAX_CALL_RETRIES + 1


def test_an_openai_retry_after_header_is_respected(monkeypatch):
    from agent import model_client as M

    waits: list[float] = []
    monkeypatch.setattr(M.time, "sleep", lambda s: waits.append(s))
    client, _ = _client(monkeypatch, [_status_error(429, {"retry-after": "17"}), _Ok()])
    monkeypatch.setattr(M.time, "sleep", lambda s: waits.append(s))
    client.call(system="s", messages=[], tools=None)
    assert waits and waits[0] == 17.0


def test_replay_mode_never_retries_or_sleeps(monkeypatch):
    """Cassette replay must stay free and deterministic."""
    from agent.model_client import ModelClient

    c = ModelClient(mode="replay", cassette="wide-refactor.openai.gpt-5.6-luna")
    monkeypatch.setattr(
        "agent.model_client.time.sleep",
        lambda *_: pytest.fail("replay slept"),
    )
    assert c.call(system="s", messages=[], tools=None) is not None


# -- the lifecycle: review persisted BEFORE the post ----------------------
#
# The architectural requirement. `worker.run_one` runs the model with
# post=False, commits the result with `record_result(state="post_pending")`, and
# only then talks to GitHub. A post failure therefore costs a retried POST, not
# a re-run of the pipeline.


class FakeStore:
    """Stands in for storage.runs, recording the calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.rows: dict = {}

    # Every fenced call returns whether it owned the row. These say True -
    # the single-worker happy path - and **kwargs keeps them from breaking on
    # the next additive signature change rather than on what is being tested.
    def record_result(self, run_id, result, *, state, **kw):
        self.calls.append(("record_result", state))
        self.rows[run_id] = {"payload": result.payload, "state": state}
        return True

    def mark_posted(self, run_id, *, state, posted, **kw):
        self.calls.append(("mark_posted", state, posted))
        return True

    def requeue_post(self, run_id, *, error, delay_s=0.0, **kw):
        self.calls.append(("requeue_post", round(delay_s, 3)))
        return True

    def requeue(self, run_id, *, error, delay_s=0.0, **kw):
        self.calls.append(("requeue", round(delay_s, 3)))
        return True

    def mark(self, run_id, state, *, error=None, **kw):
        self.calls.append(("mark", state, error))
        return True

    def heartbeat(self, *a, **k):
        return True

    def is_cancelled(self, *a, **k):
        return False


class FakeResult:
    status = "published"
    dry_run = False
    head_sha = "a" * 40
    payload = {"event": "COMMENT", "body": "b <!-- auto-pr:k -->", "comments": []}
    grounding = {"grounded": 1}
    anchoring = {"inline": 1}


@pytest.fixture
def worker(monkeypatch):
    import worker.main as W

    store = FakeStore()
    monkeypatch.setattr(W, "R", store)
    monkeypatch.setattr(W, "token_provider_for", lambda row: (lambda: TOKEN))
    monkeypatch.setattr(W.get_settings(), "max_attempts", 3, raising=False)
    return W, store


ROW = {"id": "r1", "owner": "o", "repo": "r", "pr_number": 1, "attempts": 1,
       "dry_run": False, "head_sha": "a" * 40}


def _patch_review(monkeypatch, *, post_effect=None, on_review=None):
    """Fake agent.review so no model and no GitHub call happens."""
    import agent.review as AR

    seen = {"reviews": 0, "posts": 0, "post_kwarg": None, "kwargs": {}}

    # **kwargs, not a fixed signature: this double exists to record what the
    # worker passes, and a literal copy of review_pr's parameters means every
    # additive change to it fails here as a TypeError rather than as whatever
    # the test is actually about.
    def fake_review_pr(owner, repo, number, token, **kwargs):
        seen["reviews"] += 1
        seen["post_kwarg"] = kwargs.get("post", True)
        seen["kwargs"] = kwargs
        if on_review:
            on_review()
        return FakeResult()

    def fake_post_payload(owner, repo, number, token, payload, head_sha):
        seen["posts"] += 1
        if post_effect:
            raise post_effect
        return True

    monkeypatch.setattr(AR, "review_pr", fake_review_pr)
    monkeypatch.setattr(AR, "post_payload", fake_post_payload)
    return seen


def test_the_review_is_persisted_before_the_post(worker, monkeypatch):
    W, store = worker
    seen = _patch_review(monkeypatch)
    W.run_one(dict(ROW))

    kinds = [c[0] for c in store.calls]
    assert kinds.index("record_result") < kinds.index("mark_posted")
    assert ("record_result", "post_pending") in store.calls
    assert seen["post_kwarg"] is False      # the pipeline does not post


def test_the_worker_passes_the_attempt_it_is_executing(worker, monkeypatch):
    """Only names the trace file, but a wrong value means two attempts of one
    run overwrite each other when traces are on."""
    W, store = worker
    seen = _patch_review(monkeypatch)
    W.run_one({**ROW, "attempts": 2})

    assert seen["kwargs"]["attempt"] == 2


def test_a_post_failure_keeps_the_review_and_retries_only_the_post(worker, monkeypatch):
    W, store = worker
    seen = _patch_review(
        monkeypatch, post_effect=TransientError("429", code=429, retry_after=30)
    )
    W.run_one(dict(ROW))

    assert ("record_result", "post_pending") in store.calls   # review kept
    assert ("requeue_post", 30.0) in store.calls              # post-only retry
    assert not any(c[0] == "requeue" for c in store.calls)     # NOT a full re-run
    assert seen["reviews"] == 1


def test_resuming_a_pending_post_does_not_run_the_model(worker, monkeypatch):
    W, store = worker
    seen = _patch_review(monkeypatch)
    W.post_pending({**ROW, "payload": FakeResult.payload, "state": "running"})

    assert seen["reviews"] == 0                 # the expensive path never ran
    assert seen["posts"] == 1
    assert ("mark_posted", "published", True) in store.calls


def test_a_successful_retry_posts_exactly_once(worker, monkeypatch):
    W, store = worker
    seen = _patch_review(monkeypatch)
    row = {**ROW, "payload": FakeResult.payload}
    W.post_pending(row)
    assert seen["posts"] == 1
    assert len([c for c in store.calls if c[0] == "mark_posted"]) == 1


def test_a_pending_post_that_keeps_failing_is_eventually_dead_lettered(worker, monkeypatch):
    W, store = worker
    _patch_review(monkeypatch, post_effect=TransientError("429", code=429))
    W.post_pending({**ROW, "attempts": 99, "payload": FakeResult.payload})
    assert any(c[0] == "mark" and c[1] == "failed" for c in store.calls)


def test_a_pending_post_with_no_payload_fails_loudly(worker, monkeypatch):
    W, store = worker
    _patch_review(monkeypatch)
    W.post_pending({**ROW, "payload": None})
    assert ("mark", "failed", "post_pending with no persisted payload") in store.calls


def test_a_transient_failure_before_persistence_is_a_delayed_requeue(worker, monkeypatch):
    W, store = worker
    _patch_review(
        monkeypatch,
        on_review=lambda: (_ for _ in ()).throw(
            TransientError("clone blip", retry_after=12)),
    )
    W.run_one(dict(ROW))

    assert ("requeue", 12.0) in store.calls
    assert not any(c[0] == "record_result" for c in store.calls)


def test_a_dry_run_never_posts(worker, monkeypatch):
    W, store = worker
    seen = _patch_review(monkeypatch)

    class Dry(FakeResult):
        dry_run = True

    import agent.review as AR
    monkeypatch.setattr(AR, "review_pr",
                        lambda *a, **k: (seen.__setitem__("reviews", 1), Dry())[1])
    W.run_one(dict(ROW))
    assert seen["posts"] == 0
    assert ("mark_posted", "published", False) in store.calls


def test_no_credential_reaches_persisted_retry_state(worker, monkeypatch):
    W, store = worker
    _patch_review(
        monkeypatch,
        post_effect=TransientError(f"GitHub 429 for {TOKEN[:0]}secondary limit",
                                   code=429, retry_after=9),
    )
    W.run_one(dict(ROW))
    blob = repr(store.calls) + repr(store.rows)
    assert TOKEN not in blob
    assert "x-access-token" not in blob


# -- idempotency is the existing scheme, reused ---------------------------


def test_the_retried_post_uses_the_same_idempotency_key():
    from agent.review import idempotency_key

    a = idempotency_key("o", "r", 1, "a" * 40)
    b = idempotency_key("o", "r", 1, "a" * 40)
    assert a == b and len(a) == 16


def test_the_key_changes_with_the_head_sha_only():
    from agent.review import idempotency_key

    base = idempotency_key("o", "r", 1, "a" * 40)
    assert idempotency_key("o", "r", 1, "b" * 40) != base
    assert idempotency_key("o", "r", 2, "a" * 40) != base
    assert idempotency_key("o", "r2", 1, "a" * 40) != base


def test_a_retried_post_skips_when_the_marker_is_already_there(monkeypatch):
    """The existing marker scheme makes the retry a no-op, not a duplicate."""
    import agent.review as AR

    monkeypatch.setattr(AR, "already_reviewed", lambda *a: True)
    monkeypatch.setattr(AR, "post_review",
                        lambda *a: pytest.fail("posted a duplicate review"))
    assert AR.post_payload("o", "r", 1, TOKEN, {"body": "b"}, "a" * 40) is False


def test_a_retried_post_posts_when_the_marker_is_absent(monkeypatch):
    import agent.review as AR

    posts: list = []
    monkeypatch.setattr(AR, "already_reviewed", lambda *a: False)
    monkeypatch.setattr(AR, "post_review", lambda *a: posts.append(a))
    assert AR.post_payload("o", "r", 1, TOKEN, {"body": "b"}, "a" * 40) is True
    assert len(posts) == 1


# -- the poll loop actually calls the reaper ------------------------------
#
# The SQL is verified against real Postgres in test_retry_storage.py. What that
# cannot show is that `worker.main()` ever runs it: a reaper nobody calls is the
# same as no reaper, and the crash-loop row stays `running` forever.


def test_the_poll_loop_reaps_before_claiming(monkeypatch):
    import worker.main as W

    order: list[str] = []

    class Store:
        def reap_exhausted(self, *, max_attempts):
            order.append(f"reap({max_attempts})")
            return ["dead-run-1"]

        def claim(self, *, lease_s, worker_id, max_attempts):
            order.append(f"claim({max_attempts})")
            W._stop.set()               # one pass, then drain
            return None

    monkeypatch.setattr(W, "R", Store())
    monkeypatch.setattr(W, "init_db", lambda: None)
    monkeypatch.setattr(W, "close_pool", lambda: None)
    monkeypatch.setattr(W._stop, "wait", lambda *_: True)
    W._stop.clear()
    try:
        W.main()
    finally:
        W._stop.clear()

    assert order == ["reap(3)", "claim(3)"], order


def test_the_poll_loop_passes_max_attempts_from_settings(monkeypatch):
    import worker.main as W
    from config import get_settings

    seen: dict = {}

    class Store:
        def reap_exhausted(self, *, max_attempts):
            seen["reap"] = max_attempts
            return []

        def claim(self, *, lease_s, worker_id, max_attempts):
            seen["claim"] = max_attempts
            W._stop.set()
            return None

    monkeypatch.setattr(W, "R", Store())
    monkeypatch.setattr(W, "init_db", lambda: None)
    monkeypatch.setattr(W, "close_pool", lambda: None)
    monkeypatch.setattr(W._stop, "wait", lambda *_: True)
    W._stop.clear()
    try:
        W.main()
    finally:
        W._stop.clear()

    assert seen["reap"] == seen["claim"] == get_settings().max_attempts


def test_a_db_blip_in_the_reaper_does_not_kill_the_worker(monkeypatch):
    """The reaper shares the claim's try/except; a blip must back off, not exit."""
    import worker.main as W

    calls = {"n": 0}

    class Store:
        def reap_exhausted(self, *, max_attempts):
            calls["n"] += 1
            if calls["n"] == 1:
                raise TransientError("database connection lost")
            W._stop.set()
            return []

        def claim(self, **kw):
            W._stop.set()
            return None

    monkeypatch.setattr(W, "R", Store())
    monkeypatch.setattr(W, "init_db", lambda: None)
    monkeypatch.setattr(W, "close_pool", lambda: None)
    monkeypatch.setattr(W._stop, "wait", lambda *_: True)
    W._stop.clear()
    try:
        W.main()            # must return normally, not raise
    finally:
        W._stop.clear()

    assert calls["n"] >= 2   # it came back for a second pass
