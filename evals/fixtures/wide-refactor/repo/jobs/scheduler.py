"""Enqueues due jobs in batches."""

from __future__ import annotations

from store.base import Record, Store


class Scheduler:
    def __init__(self, store: Store, batch: int = 100) -> None:
        self._store = store
        self._batch = batch

    def enqueue(self, jobs: list[dict]) -> int:
        records = [
            Record(key=job["id"], payload=job["body"].encode()) for job in jobs
        ]
        for start in range(0, len(records), self._batch):
            self._store.put_many(records[start : start + self._batch])
        return len(records)
