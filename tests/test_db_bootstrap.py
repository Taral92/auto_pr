"""Schema bootstrap is safe when API and worker start together."""

from contextlib import contextmanager

import pytest

from storage import db


class _Connection:
    def __init__(self, fail_schema=False):
        self.calls = []
        self.fail_schema = fail_schema

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        if self.fail_schema and statement == db.SCHEMA:
            raise RuntimeError("schema failed")


class _Pool:
    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def connection(self):
        yield self._connection


def test_schema_bootstrap_is_serialized_with_an_advisory_lock(monkeypatch):
    conn = _Connection()
    monkeypatch.setattr(db, "pool", lambda: _Pool(conn))

    db.init_db()

    assert conn.calls == [
        ("SELECT pg_advisory_lock(hashtext(%s))", (db.SCHEMA_LOCK_NAME,)),
        (db.SCHEMA, None),
        ("SELECT pg_advisory_unlock(hashtext(%s))", (db.SCHEMA_LOCK_NAME,)),
    ]


def test_schema_bootstrap_releases_its_lock_when_schema_setup_fails(monkeypatch):
    conn = _Connection(fail_schema=True)
    monkeypatch.setattr(db, "pool", lambda: _Pool(conn))

    with pytest.raises(RuntimeError, match="schema failed"):
        db.init_db()

    assert conn.calls[-1] == (
        "SELECT pg_advisory_unlock(hashtext(%s))", (db.SCHEMA_LOCK_NAME,)
    )
