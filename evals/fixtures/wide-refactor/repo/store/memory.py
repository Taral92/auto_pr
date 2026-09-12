"""In-memory Store, used by the tests."""

from __future__ import annotations

from .base import Record, Store


class MemoryStore(Store):
    def __init__(self) -> None:
        self._rows: dict[str, Record] = {}

    def put_many(self, records: list[Record]) -> None:
        staged = dict(self._rows)
        for record in records:
            staged[record.key] = record
        self._rows = staged

    def get(self, key: str) -> Record | None:
        return self._rows.get(key)

    def delete_many(self, keys: list[str]) -> None:
        missing = [k for k in keys if k not in self._rows]
        if missing:
            raise KeyError(missing[0])
        for key in keys:
            del self._rows[key]
