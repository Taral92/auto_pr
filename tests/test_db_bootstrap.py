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


def test_the_pool_validates_connections_before_handing_them_out(monkeypatch):
    """A pooler that closes idle connections leaves dead ones in the pool.

    Without `check`, the first caller after a quiet period always eats
    "SSL error: unexpected eof while reading" - observed as a 500 from
    /healthz and a burnt worker attempt, once per idle period.
    """
    from psycopg_pool import ConnectionPool

    seen = {}

    class _FakePool:
        # the real attribute, so db.pool() reads what production reads
        check_connection = ConnectionPool.check_connection

        def __init__(self, conninfo, **kw):
            seen.update(kw)

    monkeypatch.setattr(db, "ConnectionPool", _FakePool)
    monkeypatch.setattr(db, "_pool", None)
    db.pool()

    assert seen["check"] is ConnectionPool.check_connection


def test_schema_bootstrap_releases_its_lock_when_schema_setup_fails(monkeypatch):
    conn = _Connection(fail_schema=True)
    monkeypatch.setattr(db, "pool", lambda: _Pool(conn))

    with pytest.raises(RuntimeError, match="schema failed"):
        db.init_db()

    assert conn.calls[-1] == (
        "SELECT pg_advisory_unlock(hashtext(%s))", (db.SCHEMA_LOCK_NAME,)
    )
