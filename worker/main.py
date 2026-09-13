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


def _owned(applied: bool, run_id: str, what: str) -> bool:
    """Report a fenced write that did not apply, and pass the verdict on.

    Every fenced storage call returns whether it matched the row. Losing the
    lease is not an error - another worker legitimately owns this run now - but
    it must be visible, and it must never read as success.
    """
    if not applied:
        print(f"[{WORKER_ID}] {run_id} {what} skipped: lease lost to another worker")
    return applied


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

    A `post_pending` row IS leased while it is being posted - `claim` sets
    state='running' and `worker_id` for it exactly as for fresh work, and hands
    the previous state back as `prior_state`. So every write below is fenced
    like any other owned mutation. The duplicate-post defence at GitHub remains
    the idempotency marker, which is what makes a lost lease here cost nothing.
    """
    from agent.review import post_payload

    s = get_settings()
    run_id = row["id"]
    payload = row.get("payload")
    if not payload:
        _owned(
            R.mark(run_id, "failed", worker_id=WORKER_ID,
                   error="post_pending with no persisted payload"),
            run_id, "mark(failed)",
        )
        return
    try:
        posted = post_payload(
            row["owner"], row["repo"], row["pr_number"],
            token_provider_for(row), payload, row.get("head_sha") or "",
        )
        # Not hardcoded 'published': this row may be a DEGRADED review whose
        # first post attempt failed. `record_result` put the reason in
        # `degraded_reason`, which survives `requeue_post` - unlike `error`,
        # which that retry overwrites with the post failure - so it is what
        # tells us which terminal state this run is owed.
        final = "degraded" if row.get("degraded_reason") else "published"
        if _owned(
            R.mark_posted(run_id, state=final, posted=posted,
                          worker_id=WORKER_ID),
            run_id, "mark_posted",
        ):
            print(f"[{WORKER_ID}] {run_id} {final} posted={posted} (no model work)")
    except TransientError as e:
        if int(row.get("attempts") or 0) < s.max_attempts:
            wait = _retry_delay(row, e)
            if _owned(
                R.requeue_post(run_id, error=str(e), delay_s=wait,
                               worker_id=WORKER_ID),
                run_id, "requeue_post",
            ):
                print(f"[{WORKER_ID}] {run_id} post failed, retry in {wait:.1f}s: {e}")
        else:
            _owned(
                R.mark(run_id, "failed", worker_id=WORKER_ID,
                       error=f"post retries exhausted: {e}"),
                run_id, "mark(failed)",
            )
    except PermanentError as e:
        _owned(R.mark(run_id, "failed", worker_id=WORKER_ID, error=str(e)),
               run_id, "mark(failed)")


def run_one(row: dict) -> None:
    from agent.review import post_payload, review_pr
    from agent.runtime import is_cancelled, run_id_var

    s = get_settings()
    run_id = row["id"]
    tok_run = run_id_var.set(run_id)
    # Extend the lease while the job is alive. Without it any review slower
    # than the lease gets reclaimed and reviewed twice; with a lease long
    # enough for the worst case, a crashed job sits stuck for that long.
    beat = threading.Event()
    # Set when a heartbeat finds the lease is no longer ours. It feeds the
    # graph's own cancel hook, so the run unwinds at the next node boundary
    # rather than working on for minutes against a run someone else owns.
    lease_lost = threading.Event()

    def heartbeat():
        while not beat.wait(s.lease_s / 3):
            if not R.heartbeat(run_id, lease_s=s.lease_s, worker_id=WORKER_ID):
                print(f"[{WORKER_ID}] {run_id} lease lost; abandoning this run")
                lease_lost.set()
                return

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    # The graph checks this between nodes, so a cancel - or a lost lease -
    # lands within one node rather than at the end of the run.
    tok_cancel = is_cancelled.set(
        lambda: lease_lost.is_set() or R.is_cancelled(run_id)
    )
    try:
        # The model pipeline runs with post=False, so nothing reaches GitHub
        # until the result is in the database. `record_result` is the commit
        # point: after it, a failure costs a retried POST, never a new review.
        result = review_pr(
            row["owner"], row["repo"], row["pr_number"],
            token_provider_for(row),
            dry_run=bool(row.get("dry_run")),
            post=False,
            # Only names the trace file, and only when traces are switched on.
            # `claim` has already incremented attempts, so this is the attempt
            # being executed - two attempts of one run cannot collide.
            attempt=int(row.get("attempts") or 0),
        )
        final = result.status or "published"
        # The fence that matters most. If another worker has claimed this run,
        # our result is stale: dropping it here is also what stops us posting a
        # second review below, because the post is the next statement.
        if not _owned(
            R.record_result(run_id, result, state="post_pending",
                            worker_id=WORKER_ID),
            run_id, "record_result",
        ):
            return
        print(f"[{WORKER_ID}] {run_id} {final} computed and persisted "
              f"grounded={result.grounding.get('grounded')} "
              f"inline={result.anchoring.get('inline')}")
        if result.dry_run or final == "failed":
            _owned(
                R.mark_posted(run_id, state=final, posted=False,
                              worker_id=WORKER_ID),
                run_id, "mark_posted",
            )
            return
        try:
            posted = post_payload(
                row["owner"], row["repo"], row["pr_number"],
                token_provider_for(row), result.payload,
                result.head_sha,
            )
            if _owned(
                R.mark_posted(run_id, state=final, posted=posted,
                              worker_id=WORKER_ID),
                run_id, "mark_posted",
            ):
                print(f"[{WORKER_ID}] {run_id} posted={posted}")
        except TransientError as e:
            wait = _retry_delay(row, e)
            if _owned(
                R.requeue_post(run_id, error=str(e), delay_s=wait,
                               worker_id=WORKER_ID),
                run_id, "requeue_post",
            ):
                print(f"[{WORKER_ID}] {run_id} review kept, post retry in "
                      f"{wait:.1f}s: {e}")
    except Cancelled:
        # A lost lease unwinds through the same cancel hook, but it is not a
        # cancellation: the run belongs to someone else now, and writing any
        # terminal state would trample it. The fence would refuse us anyway;
        # this just keeps the log honest about which of the two happened.
        if lease_lost.is_set():
            print(f"[{WORKER_ID}] {run_id} released to its new owner")
        else:
            _owned(R.mark(run_id, "cancelled", worker_id=WORKER_ID),
                   run_id, "mark(cancelled)")
    except BudgetExceeded as e:
        _owned(R.mark(run_id, "degraded", worker_id=WORKER_ID, error=str(e)),
               run_id, "mark(degraded)")
    except TransientError as e:
        if int(row.get("attempts") or 0) < s.max_attempts:
            wait = _retry_delay(row, e)
            if _owned(
                R.requeue(run_id, error=str(e), delay_s=wait,
                          worker_id=WORKER_ID),
                run_id, "requeue",
            ):
                print(f"[{WORKER_ID}] {run_id} transient, retry in {wait:.1f}s: {e}")
        else:
            _owned(
                R.mark(run_id, "failed", worker_id=WORKER_ID,
                       error=f"retries exhausted: {e}"),
                run_id, "mark(failed)",
            )
    except PermanentError as e:
        _owned(R.mark(run_id, "failed", worker_id=WORKER_ID, error=str(e)),
               run_id, "mark(failed)")
    except Exception as e:                      # a bug, not a blip - be loud
        _owned(
            R.mark(run_id, "failed", worker_id=WORKER_ID,
                   error=f"{type(e).__name__}: {e}"),
            run_id, "mark(failed)",
        )
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
    # Boot sweep. Every checkpoint thread in the local DB is orphaned: nothing
    # resumes a thread, so a thread outlives its own invoke only when a crash
    # stopped `review_pr` from deleting it. Once here, not again - this is not
    # a reaper, and it never runs against a live thread, because this process
    # is the only writer of that file and has not started reviewing yet.
    from agent.review import sweep_checkpoints

    swept = sweep_checkpoints()
    if swept:
        print(f"[{WORKER_ID}] boot sweep: dropped {swept} orphaned checkpoint thread(s)")
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
