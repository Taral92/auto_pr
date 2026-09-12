# recordsvc

A batched record store behind a small HTTP API.

    api/routes.py  ->  store.Store  ->  store/pg.py

Jobs are swept into the store by `jobs/reaper.py` and enqueued by
`jobs/scheduler.py`. Payloads are encoded by `store/codec.py`.
