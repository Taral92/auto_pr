"""The operator API is gated; the webhook is not.

`/api/runs/{id}/trace` returns the corpus - the diff plus every tool result,
i.e. the source of a private repository - and `/api/review` enqueues model work
against the operator's own GitHub token. In the documented deployment those sit
on the same port GitHub delivers webhooks to, so they cannot be open.

No database and no network: the gate rejects before any handler runs, and the
two authorised cases stub the storage layer.
"""

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import config
from api import routes
from api.main import app

SECRET = "op-s3cret-value"
OTHER = "op-WRONG-value"

GETS = [
    "/api/runs",
    "/api/runs/abc",
    "/api/runs/abc/trace",
]
POSTS = [
    ("/api/review", {"pr_url": "https://github.com/o/r/pull/1"}),
    ("/api/runs/abc/cancel", None),
]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPERATOR_SECRET", SECRET)
    config.get_settings.cache_clear()
    # init_db()/close_pool() in the lifespan would dial Postgres; these tests
    # are about the gate, so the app is exercised without the lifespan.
    with TestClient(app) as _:
        pass
    yield TestClient(app)
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """Nothing here may touch Postgres. A handler that slips past the gate
    would otherwise hang or error instead of failing the assertion."""
    for name in ("list_runs", "get_run", "findings_for", "insert_queued",
                 "set_cancel", "upsert_installation", "suspend_installation",
                 "coalesce_pr", "enqueue_coalesced"):
        monkeypatch.setattr(routes.R, name,
                            lambda *a, **k: pytest.fail(f"R.{name} reached"))


@pytest.fixture(autouse=True)
def _no_developer_dotenv(monkeypatch):
    """Settings must not inherit the real .env.

    `_client(..., None)` unsets the OPERATOR_SECRET *variable*, but Settings
    also reads the .env FILE - which .env.example tells operators to fill in.
    On a configured machine the fail-closed cases then return 401 instead of
    503, so these tests would pass in CI (no .env) and fail locally. Cut the
    file out and supply the one setting that has no default.
    """
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    monkeypatch.setenv("GITHUB_TOKEN", "pat-for-tests")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _client(monkeypatch, secret: str | None):
    if secret is None:
        monkeypatch.delenv("OPERATOR_SECRET", raising=False)
    else:
        monkeypatch.setenv("OPERATOR_SECRET", secret)
    config.get_settings.cache_clear()
    return TestClient(app)


# -- unauthenticated: every operator route is refused ----------------------


@pytest.mark.parametrize("path", GETS)
def test_unauthenticated_get_is_rejected(monkeypatch, path):
    r = _client(monkeypatch, SECRET).get(path)
    assert r.status_code == 401, path
    assert "WWW-Authenticate" in r.headers


@pytest.mark.parametrize("path,body", POSTS)
def test_unauthenticated_post_is_rejected(monkeypatch, path, body):
    r = _client(monkeypatch, SECRET).post(path, json=body)
    assert r.status_code == 401, path


def test_every_api_route_is_gated():
    """A route added later must be protected by default, not by memory."""
    ungated = [
        r.path for r in app.routes
        if getattr(r, "path", "").startswith("/api")
        and not getattr(r, "dependencies", [])
    ]
    assert ungated == []


# -- wrong / malformed credentials ----------------------------------------


@pytest.mark.parametrize("header", [
    f"Bearer {OTHER}",          # wrong secret
    "Bearer ",                  # empty token
    f"Basic {SECRET}",          # wrong scheme
    SECRET,                     # no scheme
    f"bearer {SECRET}x",        # right prefix, extra byte
    f"Bearer {SECRET[:-1]}",    # truncated
])
def test_bad_credentials_are_rejected(monkeypatch, header):
    r = _client(monkeypatch, SECRET).get("/api/runs",
                                         headers={"Authorization": header})
    assert r.status_code == 401, header


def test_a_non_ascii_credential_is_rejected_not_a_500(monkeypatch):
    """HTTP headers are ASCII, so non-ASCII arrives as raw bytes. Comparing it
    as str would raise inside hmac.compare_digest and surface as a 500."""
    r = _client(monkeypatch, SECRET).get(
        "/api/runs", headers={"Authorization": "Bearer ünicode".encode("latin-1")})
    assert r.status_code == 401


def test_the_comparison_itself_tolerates_non_ascii():
    from api.auth import _presented_matches

    assert _presented_matches("Bearer ünicode", SECRET) is False
    assert _presented_matches("Bearer " + SECRET, "sécret") is False


def test_scheme_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(routes.R, "list_runs", lambda **k: [])
    r = _client(monkeypatch, SECRET).get("/api/runs",
                                         headers={"Authorization": f"bearer {SECRET}"})
    assert r.status_code == 200


# -- correct credentials succeed ------------------------------------------


def test_correct_secret_succeeds(monkeypatch):
    monkeypatch.setattr(routes.R, "list_runs", lambda **k: [])
    r = _client(monkeypatch, SECRET).get(
        "/api/runs", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200
    assert r.json() == []


def test_correct_secret_reaches_the_trace_handler(monkeypatch):
    """The most sensitive route still works for an authorised caller."""
    monkeypatch.setattr(routes.R, "get_run",
                        lambda rid: {"corpus": [{"source": "diff", "text": "x"}],
                                     "trace": []})
    r = _client(monkeypatch, SECRET).get(
        "/api/runs/abc/trace", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 200
    assert r.json()["corpus"] == [{"source": "diff", "text": "x"}]


# -- fail closed when unconfigured ----------------------------------------


@pytest.mark.parametrize("path", GETS)
def test_unconfigured_secret_disables_the_api(monkeypatch, path):
    """Unset must mean closed, not open. No 'dev mode' opens this."""
    r = _client(monkeypatch, None).get(path)
    assert r.status_code == 503


def test_unconfigured_rejects_even_a_plausible_bearer(monkeypatch):
    r = _client(monkeypatch, None).get(
        "/api/runs", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_unconfigured_rejects_an_empty_bearer(monkeypatch):
    """An empty configured secret must not be matchable by an empty token."""
    r = _client(monkeypatch, "").get(
        "/api/runs", headers={"Authorization": "Bearer "})
    assert r.status_code == 503


# -- the webhook is untouched ---------------------------------------------


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


WEBHOOK_SECRET = "whsec"


def test_webhook_needs_no_operator_credentials(monkeypatch):
    """GitHub cannot send a bearer token; the webhook must stay open to HMAC."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = json.dumps({"action": "labeled"}).encode()
    r = _client(monkeypatch, SECRET).post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body, WEBHOOK_SECRET),
                 "X-GitHub-Event": "pull_request"},
    )
    assert r.status_code == 202       # valid HMAC, event ignored


def test_webhook_still_rejects_a_bad_signature(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b'{"action":"opened"}'
    r = _client(monkeypatch, SECRET).post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body, "wrong"),
                 "X-GitHub-Event": "pull_request"},
    )
    assert r.status_code == 401


def test_webhook_rejects_a_too_large_content_length_before_buffering(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    r = _client(monkeypatch, SECRET).post(
        "/webhook", content=b"",
        headers={"Content-Length": str(routes.MAX_WEBHOOK_BYTES + 1)},
    )
    assert r.status_code == 413


def test_webhook_does_not_buffer_a_too_large_content_length():
    class OversizedRequest:
        headers = {"content-length": str(routes.MAX_WEBHOOK_BYTES + 1)}

        async def body(self):
            pytest.fail("oversized webhook body was buffered")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(routes.webhook(OversizedRequest()))
    assert excinfo.value.status_code == 413


def test_operator_secret_does_not_authenticate_the_webhook(monkeypatch):
    """The two secrets are separate; one must not stand in for the other."""
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    body = b'{"action":"opened"}'
    r = _client(monkeypatch, SECRET).post(
        "/webhook", content=body,
        headers={"Authorization": f"Bearer {SECRET}",
                 "X-GitHub-Event": "pull_request"},
    )
    assert r.status_code == 401       # no HMAC -> still refused


def test_healthz_stays_public(monkeypatch):
    """Documented decision: container/LB probes run without credentials."""
    monkeypatch.setattr("api.routes.health", lambda: {"db": "ok"})
    r = _client(monkeypatch, SECRET).get("/healthz")
    assert r.status_code == 200


# -- the secret never leaves the process ----------------------------------


def test_the_secret_never_appears_in_any_response(monkeypatch, capsys):
    c = _client(monkeypatch, SECRET)
    bodies = [c.get(p).text for p in GETS]
    bodies += [c.post(p, json=b).text for p, b in POSTS]
    bodies.append(c.get("/api/runs",
                        headers={"Authorization": f"Bearer {OTHER}"}).text)
    bodies.append(c.get("/openapi.json").text)
    for body in bodies:
        assert SECRET not in body
        assert OTHER not in body          # nor the presented credential
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err


def test_unconfigured_message_names_no_value(monkeypatch):
    r = _client(monkeypatch, None).get("/api/runs")
    assert SECRET not in r.text
    assert "OPERATOR_SECRET" in r.text     # names the VARIABLE, not a value
