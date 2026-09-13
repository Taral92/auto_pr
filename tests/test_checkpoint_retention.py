"""Checkpoint and trace retention.

LangGraph checkpoints the WHOLE state at every superstep, and `messages` and
`corpus` grow all run, so the cost is quadratic in turns: a 9-turn review of a
277KB diff measured 26-31MB, and five of them shared a 145MB file. Nothing ever
read it again - no production path resumes a thread, because both `app.invoke`
calls pass a complete input and none passes `None`.

So a thread is dead the moment its invoke returns, which is what makes
delete-on-exit safe and a boot sweep sufficient. The one thing that must NOT
depend on any of this is the C2 post-retry path, and it does not: `post_pending`
reads the payload from Postgres and never touches a checkpointer.
"""

import json
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

import agent.review as AR
from core.errors import TransientError

TOKEN = "ghs_SECRET_TOKEN_VALUE"
SHA = "a" * 40
SMALL = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    "@@ -1,2 +1,2 @@\n def handler():\n-    return 1\n+    return 2\n"
)
SUBMISSION = {"summary": "Nothing found.", "findings": []}


def _threads(db) -> set[str]:
    if not db.exists():
        return set()
    c = sqlite3.connect(db)
    try:
        return {r[0] for r in c.execute("SELECT DISTINCT thread_id FROM checkpoints")}
    finally:
        c.close()


def _writes(db) -> int:
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT count(*) FROM writes").fetchone()[0]
    finally:
        c.close()


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """A real review_pr, with GitHub, the clone and the model all faked.

    `PROJECT_ROOT` and `CHECKPOINT_DB` move into tmp_path, so nothing here
    writes to the repository's own runs/ directory.
    """
    from agent import nodes
    from agent.model_client import _Response
    from agent.tools import SUBMIT_FINDINGS

    work = tmp_path / "work"
    work.mkdir()
    (work / "a.py").write_text("def handler():\n    return 2\n")

    monkeypatch.setattr(AR, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(AR, "CHECKPOINT_DB", tmp_path / "runs" / "checkpoints.db")
    monkeypatch.setattr(AR, "get_pr", lambda *a: {"head": {"sha": SHA}})
    monkeypatch.setattr(AR, "get_diff", lambda *a: SMALL)
    monkeypatch.setattr(AR, "clone_head", lambda dest, *a: None)
    monkeypatch.setattr(AR, "rmtree", lambda p: None)
    monkeypatch.setattr(AR, "already_reviewed", lambda *a: False)
    monkeypatch.setattr(AR, "posted_finding_keys", lambda *a: set())
    monkeypatch.setattr(AR, "post_review", lambda *a: None)
    monkeypatch.setattr(AR.tempfile, "mkdtemp", lambda **k: str(work))

    def submit(**_):
        return _Response({
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "content": [{"type": "tool_use", "id": "s1",
                         "name": SUBMIT_FINDINGS, "input": SUBMISSION}],
        })

    monkeypatch.setattr(nodes, "_call_model", submit)
    return tmp_path, AR.CHECKPOINT_DB


def _review(run_id, **kw):
    from agent.runtime import run_id_var

    tok = run_id_var.set(run_id)
    try:
        return AR.review_pr("o", "r", 1, TOKEN, dry_run=True, post=False, **kw)
    finally:
        run_id_var.reset(tok)


# -- delete on exit --------------------------------------------------------


def test_a_successful_review_deletes_its_checkpoint_thread(sandbox):
    _, db = sandbox
    result = _review("run-ok")

    assert result.status == "published"          # the review really ran
    assert _threads(db) == set()
    assert _writes(db) == 0                      # writes table too, not just checkpoints


def test_a_failed_review_deletes_its_checkpoint_thread(sandbox, monkeypatch):
    """The finally, not the happy path. A crash mid-graph must still clean up."""
    from agent import nodes

    _, db = sandbox
    monkeypatch.setattr(
        nodes, "_call_model",
        lambda **_: (_ for _ in ()).throw(TransientError("429 mid-run")),
    )
    with pytest.raises(TransientError):
        _review("run-boom")

    assert _threads(db) == set()


def test_cleanup_is_scoped_to_this_run_and_another_runs_thread_survives(sandbox):
    """A concurrent run's checkpoint must not be collateral damage."""
    _, db = sandbox
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.setup()
        saver.put(
            {"configurable": {"thread_id": "other-run", "checkpoint_ns": ""}},
            {"v": 1, "id": "c1", "ts": "t", "channel_values": {},
             "channel_versions": {}, "versions_seen": {}},
            {"source": "input", "step": 0},
            {},
        )
    assert "other-run" in _threads(db)

    _review("run-ok")

    assert _threads(db) == {"other-run"}


def test_sequential_reviews_do_not_grow_the_checkpoint_db(sandbox):
    """The regression for the 145MB measurement."""
    _, db = sandbox
    sizes = []
    for i in range(12):
        _review(f"run-{i}")
        sizes.append(db.stat().st_size)

    assert _threads(db) == set()
    # Bounded, not merely "smaller": the file must not track the run count.
    assert sizes[-1] <= max(sizes[0], 256 * 1024), sizes


# -- the post-retry path does not depend on checkpoints --------------------


def test_post_pending_still_works_after_the_checkpoint_is_deleted(sandbox,
                                                                  monkeypatch):
    """C2 is untouched: the retry reads the payload from Postgres, not disk."""
    import worker.main as W

    _, db = sandbox
    result = _review("run-ok")
    assert _threads(db) == set()                  # nothing left to resume from

    calls = {"posts": 0, "marked": None}
    monkeypatch.setattr(AR, "post_review",
                        lambda *a: calls.__setitem__("posts", calls["posts"] + 1))
    monkeypatch.setattr(W, "token_provider_for", lambda row: (lambda: TOKEN))
    monkeypatch.setattr(
        W, "R",
        type("S", (), {
            "mark_posted": lambda s, rid, *, state, posted: calls.__setitem__(
                "marked", (state, posted)),
            "mark": lambda s, rid, st, *, error=None: None,
            "requeue_post": lambda s, rid, *, error, delay_s=0.0: None,
        })(),
    )
    W.post_pending({"id": "run-ok", "owner": "o", "repo": "r", "pr_number": 1,
                    "attempts": 1, "head_sha": SHA, "payload": result.payload})

    assert calls["posts"] == 1
    assert calls["marked"] == ("published", True)


# -- boot sweep ------------------------------------------------------------


def test_the_boot_sweep_removes_orphaned_threads(tmp_path):
    db = tmp_path / "checkpoints.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.setup()
        for tid in ("orphan-a", "orphan-b", "orphan-c"):
            saver.put(
                {"configurable": {"thread_id": tid, "checkpoint_ns": ""}},
                {"v": 1, "id": f"c-{tid}", "ts": "t", "channel_values": {},
                 "channel_versions": {}, "versions_seen": {}},
                {"source": "input", "step": 0},
                {},
            )
    assert len(_threads(db)) == 3

    assert AR.sweep_checkpoints(db) == 3
    assert _threads(db) == set()
    assert _writes(db) == 0


def test_an_empty_boot_sweep_is_safe(tmp_path):
    missing = tmp_path / "nope.db"
    assert AR.sweep_checkpoints(missing) == 0      # no file at all
    assert not missing.exists()                    # and none created

    db = tmp_path / "checkpoints.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.setup()
    assert AR.sweep_checkpoints(db) == 0            # file, no threads
    assert AR.sweep_checkpoints(db) == 0            # and again


def test_the_sweep_reclaims_the_file_not_just_the_rows(tmp_path):
    """delete_thread frees pages for reuse; only VACUUM returns them."""
    db = tmp_path / "checkpoints.db"
    with SqliteSaver.from_conn_string(str(db)) as saver:
        saver.setup()
        for i in range(200):
            saver.put(
                {"configurable": {"thread_id": f"t{i}", "checkpoint_ns": ""}},
                {"v": 1, "id": f"c{i}", "ts": "t",
                 "channel_values": {"big": "x" * 4000},
                 "channel_versions": {}, "versions_seen": {}},
                {"source": "input", "step": 0},
                {},
            )
    before = db.stat().st_size
    AR.sweep_checkpoints(db)
    assert db.stat().st_size < before


def test_the_worker_sweeps_at_boot(monkeypatch):
    """Wired up, and before the poll loop rather than inside it."""
    import inspect

    import worker.main as W

    src = inspect.getsource(W.main)
    assert "sweep_checkpoints()" in src
    assert src.index("sweep_checkpoints()") < src.index("while not _stop")


# -- trace files -----------------------------------------------------------


def test_the_worker_writes_no_trace_by_default(sandbox):
    root, _ = sandbox
    _review("run-ok")
    assert list((root / "runs").glob("*.json")) == []


def test_the_setting_is_off_by_default():
    from config import Settings

    assert Settings.model_fields["write_trace"].default is False


def test_the_cli_still_writes_a_trace(sandbox):
    """The CLI's runs/ file is its only output; it passes write_trace=True."""
    import inspect

    from agent import cli

    assert "write_trace=True" in inspect.getsource(cli.main)

    root, _ = sandbox
    _review("run-cli", write_trace=True)
    files = list((root / "runs").glob("*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["run_id"] == "run-cli" and body["status"] == "published"


def test_two_same_second_writes_produce_distinct_files(sandbox, monkeypatch):
    """The old name was the timestamp alone, at one-second resolution."""
    root, _ = sandbox

    class FrozenClock:
        @staticmethod
        def now():
            import datetime as dt
            return dt.datetime(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(AR, "datetime", FrozenClock)
    _review("run-one", write_trace=True)
    _review("run-two", write_trace=True)

    files = sorted(f.name for f in (root / "runs").glob("*.json"))
    assert len(files) == 2, files
    assert len({json.loads((root / "runs" / f).read_text())["run_id"]
                for f in files}) == 2


def test_two_attempts_of_one_run_do_not_collide(sandbox, monkeypatch):
    root, _ = sandbox

    class FrozenClock:
        @staticmethod
        def now():
            import datetime as dt
            return dt.datetime(2026, 1, 1, 12, 0, 0)

    monkeypatch.setattr(AR, "datetime", FrozenClock)
    _review("run-same", attempt=1, write_trace=True)
    _review("run-same", attempt=2, write_trace=True)

    assert len(list((root / "runs").glob("*.json"))) == 2


def test_trace_files_are_bounded(sandbox, monkeypatch):
    root, _ = sandbox
    monkeypatch.setattr(AR.get_settings(), "max_trace_files", 5, raising=False)
    for i in range(12):
        _review(f"run-{i}", write_trace=True)

    kept = list((root / "runs").glob("*.json"))
    assert len(kept) == 5
    # The newest survive, not an arbitrary five.
    assert {json.loads(f.read_text())["run_id"] for f in kept} == {
        f"run-{i}" for i in range(7, 12)
    }


def test_pruning_never_considers_the_checkpoint_db(sandbox, monkeypatch):
    root, db = sandbox
    monkeypatch.setattr(AR.get_settings(), "max_trace_files", 1, raising=False)
    for i in range(4):
        _review(f"run-{i}", write_trace=True)

    assert db.exists()                       # the .db survived the prune
    assert len(list((root / "runs").glob("*.json"))) == 1
