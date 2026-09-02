import os
import tempfile

import pytest
from fastapi.testclient import TestClient

PR = "https://github.com/Taral92/auto_pr/pull/1"


@pytest.fixture
def client(monkeypatch):
    db = os.path.join(tempfile.mkdtemp(), "test.db")
    monkeypatch.setenv("DB_PATH", db)
    import config

    config.get_settings.cache_clear()
    from api.main import app

    # TestClient MUST be used as a context manager, or lifespan never runs and
    # init_db is skipped -> "no such table: runs".
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


def test_create_review_returns_202_and_queues(client):
    r = client.post("/api/review", json={"pr_url": PR, "dry_run": True})
    assert r.status_code == 202
    assert r.json()["run_id"]
    assert client.get("/healthz").json()["queued"] == 1


def test_api_never_runs_the_review(client):
    """The API must only write a row. If it ever executes, this goes 'running'."""
    rid = client.post("/api/review", json={"pr_url": PR, "dry_run": True}).json()["run_id"]
    assert client.get(f"/api/runs/{rid}").json()["state"] == "queued"


def test_bad_url_is_422(client):
    assert client.post("/api/review", json={"pr_url": "not-a-url"}).status_code == 422


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/trace").status_code == 404
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_list_and_detail(client):
    rid = client.post("/api/review", json={"pr_url": PR, "dry_run": True}).json()["run_id"]
    assert len(client.get("/api/runs?limit=5").json()) == 1
    d = client.get(f"/api/runs/{rid}").json()
    assert d["pr_number"] == 1 and d["dry_run"] is True
    assert d["findings"] == []


def test_cancel_sets_flag(client):
    rid = client.post("/api/review", json={"pr_url": PR, "dry_run": True}).json()["run_id"]
    assert client.post(f"/api/runs/{rid}/cancel").json()["cancel"] is True


def test_trace_empty_before_worker_runs(client):
    rid = client.post("/api/review", json={"pr_url": PR, "dry_run": True}).json()["run_id"]
    t = client.get(f"/api/runs/{rid}/trace").json()
    assert t["corpus"] is None and t["trace"] is None
