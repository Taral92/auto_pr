"""Postgres-backed Store."""

from __future__ import annotations

import psycopg

from .base import Record, Store, StoreError


class _Conflict(Exception):
    pass


class PgStore(Store):
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _write_one(self, cur, record: Record) -> None:
        cur.execute(
            "INSERT INTO records (key, payload, etag) VALUES (%s, %s, %s)"
            " ON CONFLICT (key) DO NOTHING RETURNING key",
            (record.key, record.payload, record.etag),
        )
        if cur.fetchone() is None:
            raise _Conflict(record.key)

    def put_many(self, records: list[Record]) -> None:
        written = 0
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                for record in records:
                    try:
                        self._write_one(cur, record)
                        written += 1
                    except _Conflict:
                        continue
                conn.commit()

    def get(self, key: str) -> Record | None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key, payload, etag FROM records WHERE key = %s", (key,)
                )
                row = cur.fetchone()
        return Record(row[0], row[1], row[2]) if row else None

    def delete_many(self, keys: list[str]) -> None:
        with psycopg.connect(self._dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM records WHERE key = ANY(%s)", (keys,))
                if cur.rowcount != len(keys):
                    raise StoreError(f"deleted {cur.rowcount} of {len(keys)}")
                conn.commit()
