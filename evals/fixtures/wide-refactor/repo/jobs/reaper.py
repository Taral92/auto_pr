"""Sweeps expired jobs into the archive store."""

from __future__ import annotations

import time

from store.base import Record, Store
from .backoff import next_delay


class Reaper:
    def __init__(self, store: Store, ttl_s: int = 3600) -> None:
        self._store = store
        self._ttl_s = ttl_s

    def _expired(self, jobs: list[dict]) -> list[dict]:
        cutoff = time.time() - self._ttl_s
        return [j for j in jobs if j["updated_at"] < cutoff]

    def sweep(self, jobs: list[dict]) -> int:
        swept = 0
        for job in self._expired(jobs):
            record = Record(key=job["id"], payload=job["body"].encode())
            self._store.put(record)
            swept += 1
        return swept

    def sweep_with_retry(self, jobs: list[dict], attempt: int = 0) -> int:
        try:
            return self.sweep(jobs)
        except Exception:
            time.sleep(next_delay(attempt))
            raise
