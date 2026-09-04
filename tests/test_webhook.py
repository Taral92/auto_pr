"""Webhook signature + event filtering. No database, no network."""

import hashlib
import hmac

from gh.webhook import should_review, verify

SECRET = "s3cret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature():
    b = b'{"action":"opened"}'
    assert verify(b, sign(b), SECRET)


def test_tampered_body_rejected():
    b = b'{"action":"opened"}'
    assert not verify(b + b" ", sign(b), SECRET)


def test_wrong_secret_rejected():
    b = b'{"action":"opened"}'
    assert not verify(b, sign(b, "other"), SECRET)


def test_missing_signature_rejected():
    assert not verify(b"{}", None, SECRET)


def test_unset_secret_rejects_everything():
    """An unconfigured secret must fail closed, never open."""
    b = b"{}"
    assert not verify(b, sign(b), "")


def _pr(action="opened", draft=False, user_type="User"):
    return {"action": action, "pull_request": {"draft": draft,
                                               "user": {"type": user_type}}}


def test_reviews_the_three_actions():
    for a in ("opened", "synchronize", "reopened"):
        assert should_review("pull_request", _pr(a))


def test_ignores_other_actions():
    for a in ("closed", "labeled", "assigned", "edited"):
        assert not should_review("pull_request", _pr(a))


def test_ignores_other_events():
    assert not should_review("push", _pr())
    assert not should_review("issues", _pr())


def test_ignores_drafts():
    assert not should_review("pull_request", _pr(draft=True))


def test_ignores_bots():
    """Otherwise the agent reviews its own PRs, and every dependabot bump."""
    assert not should_review("pull_request", _pr(user_type="Bot"))
