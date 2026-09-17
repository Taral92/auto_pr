"""PostgreSQL connection pool and schema bootstrap."""

from __future__ import annotations

from pathlib import Path

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from config import get_settings

SCHEMA = (Path(__file__).with_name("schema.sql")).read_text()
SCHEMA_LOCK_NAME = "auto-pr-schema-bootstrap"

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """One pool per process. Opened lazily so importing storage never dials."""
    global _pool
    if _pool is None:
        s = get_settings()
        _pool = ConnectionPool(
            s.database_url,
            min_size=1,
            max_size=s.db_pool_size,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "prepare_threshold": None,
            },
            # Validate a connection before handing it out. A pooler that closes
            # idle connections - Supabase does - leaves dead ones in the pool,
            # and without this the first caller after a quiet period always
            # eats "SSL error: unexpected eof while reading". That surfaced as
            # a 500 from /healthz and as a failed attempt in the worker, one
            # per idle period. `check` discards and replaces the connection
            # instead. Call sites still translate OperationalError: a
            # connection can also die mid-query, which no check can prevent.
            check=ConnectionPool.check_connection,
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def init_db() -> None:
    with pool().connection() as conn:
        # API and worker boot independently and both call this function.  The
        # schema is idempotent, but concurrent ALTER TABLE statements can still
        # deadlock.  Hold a session lock across the entire bootstrap so the
        # second process waits for the first instead of racing it.
        conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (SCHEMA_LOCK_NAME,))
        try:
            conn.execute(SCHEMA)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (SCHEMA_LOCK_NAME,))


def health() -> dict:
    with pool().connection() as conn:
        version = conn.execute("SHOW server_version").fetchone()["server_version"]
        counts = {
            r["state"]: r["n"]
            for r in conn.execute(
                "SELECT state, count(*) AS n FROM runs GROUP BY state"
            ).fetchall()
        }
    return {"db": "ok", "server_version": version, "states": counts}
