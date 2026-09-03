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
