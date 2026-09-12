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

from config import get_settings
from core.backoff import delay_for
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


def _retry_delay(row: dict, exc: TransientError) -> float:
    """Wait before the next attempt: the service's own hint, else backoff."""
    return delay_for(
        int(row.get("attempts") or 1),
        retry_after=getattr(exc, "retry_after", None),
    )


def post_pending(row: dict) -> None:
    """Post a review that is already computed and persisted.

    This path never touches the model. It exists because a transient GitHub
    failure at post time used to discard a finished review and re-run the whole
    pipeline - paying for every turn a second and third time to answer one API
    call that had failed.
    """
    from agent.review import post_payload

    s = get_settings()
    run_id = row["id"]
    payload = row.get("payload")
    if not payload:
        R.mark(run_id, "failed", error="post_pending with no persisted payload")
        return
    try:
        posted = post_payload(
            row["owner"], row["repo"], row["pr_number"],
            token_provider_for(row), payload, row.get("head_sha") or "",
        )
        R.mark_posted(run_id, state="published", posted=posted)
        print(f"[{WORKER_ID}] {run_id} posted={posted} (no model work)")
    except TransientError as e:
        if int(row.get("attempts") or 0) < s.max_attempts:
            wait = _retry_delay(row, e)
            R.requeue_post(run_id, error=str(e), delay_s=wait)
            print(f"[{WORKER_ID}] {run_id} post failed, retry in {wait:.1f}s: {e}")
        else:
            R.mark(run_id, "failed", error=f"post retries exhausted: {e}")
    except PermanentError as e:
        R.mark(run_id, "failed", error=str(e))


def run_one(row: dict) -> None:
    from agent.review import post_payload, review_pr
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
        # The model pipeline runs with post=False, so nothing reaches GitHub
        # until the result is in the database. `record_result` is the commit
        # point: after it, a failure costs a retried POST, never a new review.
        result = review_pr(
            row["owner"], row["repo"], row["pr_number"],
            token_provider_for(row),
            dry_run=bool(row.get("dry_run")),
            post=False,
        )
        final = result.status or "published"
        R.record_result(run_id, result, state="post_pending")
        print(f"[{WORKER_ID}] {run_id} {final} computed and persisted "
              f"grounded={result.grounding.get('grounded')} "
              f"inline={result.anchoring.get('inline')}")
        if result.dry_run or final == "failed":
            R.mark_posted(run_id, state=final, posted=False)
            return
        try:
            posted = post_payload(
                row["owner"], row["repo"], row["pr_number"],
                token_provider_for(row), result.payload,
                result.head_sha,
            )
            R.mark_posted(run_id, state=final, posted=posted)
            print(f"[{WORKER_ID}] {run_id} posted={posted}")
        except TransientError as e:
            wait = _retry_delay(row, e)
            R.requeue_post(run_id, error=str(e), delay_s=wait)
            print(f"[{WORKER_ID}] {run_id} review kept, post retry in "
                  f"{wait:.1f}s: {e}")
    except Cancelled:
        R.mark(run_id, "cancelled")
    except BudgetExceeded as e:
        R.mark(run_id, "degraded", error=str(e))
    except TransientError as e:
        if int(row.get("attempts") or 0) < s.max_attempts:
            wait = _retry_delay(row, e)
            R.requeue(run_id, error=str(e), delay_s=wait)
            print(f"[{WORKER_ID}] {run_id} transient, retry in {wait:.1f}s: {e}")
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
                # Close out anything a crashed worker abandoned at the ceiling.
                # The claim predicate already refuses to pick those rows up, so
                # without this they would sit `running` forever: never retried,
                # never resolved, and reported as in-flight by every operator
                # view. Cheap - one indexed UPDATE that normally matches nothing.
                for reaped in R.reap_exhausted(max_attempts=s.max_attempts):
                    print(f"[{WORKER_ID}] {reaped} reaped: attempts exhausted")
                row = R.claim(
                    lease_s=s.lease_s,
                    worker_id=WORKER_ID,
                    max_attempts=s.max_attempts,
                )
            except TransientError as e:
                # A DB blip must not kill the worker. Back off and retry the
                # claim; the pool reconnects on the next attempt.
                print(f"[{WORKER_ID}] claim failed, retrying: {e}")
                _stop.wait(s.poll_interval_s)
                continue
            if row is None:
                _stop.wait(s.poll_interval_s)
                continue
            # `prior_state` is what the row was before the claim set it to
            # 'running'. post_pending means the review is already computed and
            # persisted, so this claim owes GitHub a post and nothing else.
            if row.get("prior_state") == "post_pending":
                post_pending(row)
            else:
                run_one(row)
    finally:
        close_pool()
        print(f"[{WORKER_ID}] stopped")


if __name__ == "__main__":
    main()
