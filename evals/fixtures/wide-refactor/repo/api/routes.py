"""HTTP surface. Accepts a batch, hands it to the store, returns 202."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from store.base import Record, Store, StoreError

router = APIRouter()
_store: Store | None = None


def bind(store: Store) -> None:
    global _store
    _store = store


@router.post("/records", status_code=202)
def create(items: list[dict]) -> dict:
    if _store is None:
        raise HTTPException(503, "store not bound")
    records = [Record(key=i["key"], payload=i["payload"].encode()) for i in items]
    try:
        _store.put_many(records)
    except StoreError as e:
        raise HTTPException(409, str(e)) from e
    return {"accepted": len(records)}
