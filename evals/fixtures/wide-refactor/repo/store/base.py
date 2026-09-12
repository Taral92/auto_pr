"""The storage contract. One place defines what a write means."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class StoreError(Exception):
    """A write did not happen. The store is unchanged."""


@dataclass(frozen=True)
class Record:
    key: str
    payload: bytes
    etag: str | None = None


class Store(ABC):
    """A record store.

    `put_many` replaced the old single-record `put`. The batch form is
    all-or-nothing: when it returns, either every record in the batch is
    durable or none of them are and it raised. Callers depend on that -
    it is the reason none of them run a reconciliation pass afterwards.
    """

    @abstractmethod
    def put_many(self, records: list[Record]) -> None:
        """Write every record, or none of them. Raise StoreError otherwise."""

    @abstractmethod
    def get(self, key: str) -> Record | None:
        """Return the record for `key`, or None."""

    @abstractmethod
    def delete_many(self, keys: list[str]) -> None:
        """Remove every key, or none of them."""
