"""Job worker.

    claim (FOR UPDATE SKIP LOCKED) -> review -> record

Run as many replicas as you like: SKIP LOCKED means two workers never claim
the same row, and an expired lease means a crashed worker's job is picked up
by the next one instead of sitting stuck forever.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time

from config import get_settings
from core.errors import BudgetExceeded, Cancelled, PermanentError, TransientError
from gh.auth import TOKENS, static_provider
from storage import runs as R
from storage.db import close_pool, init_db

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
_stop = threading.Event()


def _shutdown(*_):
    print(f"[{WORKER_ID}] draining; will exit after the current job")
    _stop.set()


def token_provider_for(row: dict):
    """App installation token when we have one, PAT otherwise (CLI-queued runs)."""
    inst = row.get("installation_id")
    if inst:
        return TOKENS.provider(int(inst))
    return static_provider(get_settings().github_token.get_secret_value())


def run_one(row: dict) -> None:
    from agent.review import review_pr
    from agent.runtime import is_cancelled, run_id_var

    s = get_settings()
    run_id = row["id"]
    tok_run = run_id_var.set(run_id)
    # The graph checks this between nodes, so a cancel lands within one node
    # rather than at the end of the run.
    tok_cancel = is_cancelled.set(lambda: R.is_cancelled(run_id))

    # Extend the lease while the job is alive. Without it any review slower
    # than the lease gets reclaimed and reviewed twice; with a lease long
    # enough for the worst case, a crashed job sits stuck for that long.
    beat = threading.Event()

    def heartbeat():
        while not beat.wait(s.lease_s / 3):
            R.heartbeat(run_id, lease_s=s.lease_s)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    try:
        result = review_pr(
            row["owner"], row["repo"], row["pr_number"],
            token_provider_for(row),
            dry_run=bool(row.get("dry_run")),
        )
        R.record_result(run_id, result, state=result.status or "published")
        print(f"[{WORKER_ID}] {run_id} {result.status} "
              f"grounded={result.grounding.get('grounded')} "
              f"inline={result.anchoring.get('inline')}")
    except Cancelled:
        R.mark(run_id, "cancelled")
    except BudgetExceeded as e:
        R.mark(run_id, "degraded", error=str(e))
    except TransientError as e:
        if int(row.get("attempts") or 0) < s.max_attempts:
            R.requeue(run_id, error=str(e))
            print(f"[{WORKER_ID}] {run_id} transient, requeued: {e}")
        else:
            R.mark(run_id, "failed", error=f"retries exhausted: {e}")
    except PermanentError as e:
        R.mark(run_id, "failed", error=str(e))
    except Exception as e:                      # a bug, not a blip - be loud
        R.mark(run_id, "failed", error=f"{type(e).__name__}: {e}")
        print(f"[{WORKER_ID}] {run_id} UNEXPECTED {type(e).__name__}: {e}")
    finally:
        beat.set()
        is_cancelled.reset(tok_cancel)
        run_id_var.reset(tok_run)


def main() -> None:
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    s = get_settings()
    if s.lease_s <= s.max_wall_clock_s:
        raise SystemExit(
            f"LEASE_S ({s.lease_s}) must exceed MAX_WALL_CLOCK_S "
            f"({s.max_wall_clock_s}), or a slow run is reclaimed and "
            f"reviewed twice."
        )
    init_db()
    print(f"[{WORKER_ID}] up; lease={s.lease_s}s")
    try:
        while not _stop.is_set():
            try:
                row = R.claim(lease_s=s.lease_s, worker_id=WORKER_ID)
            except TransientError as e:
                # A DB blip must not kill the worker. Back off and retry the
                # claim; the pool reconnects on the next attempt.
                print(f"[{WORKER_ID}] claim failed, retrying: {e}")
                _stop.wait(s.poll_interval_s)
                continue
            if row is None:
                _stop.wait(s.poll_interval_s)
                continue
            run_one(row)
    finally:
        close_pool()
        print(f"[{WORKER_ID}] stopped")


if __name__ == "__main__":
    main()
