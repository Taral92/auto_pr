"""Run queue and history.

The claim query is the whole concurrency story:

    SELECT ... FOR UPDATE SKIP LOCKED

`SKIP LOCKED` is why this scales past one worker. Without it, N workers all
block on the same oldest row and you have a serial queue wearing a pool's
clothes. With it, each worker takes the oldest row nobody else holds.

Ownership fencing
-----------------
`claim` writes `worker_id`, and that column is the lease. Every mutation a
worker performs WHILE it holds a lease carries its own id into the WHERE
clause and returns whether the row matched:

    WHERE id = %(run_id)s AND worker_id = %(worker_id)s

Without it, a worker whose lease expired could still finish. The sequence is
real: worker A stalls long enough for its lease to lapse - a starved heartbeat
thread, a paused container - B claims the row and starts reviewing, then A wakes
up and calls `record_result` and `mark_posted`. A overwrites B's row and posts a
second review. The fence makes A's UPDATE match zero rows, and `False` comes
back so the caller can stop instead of carrying on believing it succeeded.

Ownership is `worker_id`, not `leased_until > now()`. A lapsed lease that
nobody else has claimed is not a transfer: the original owner is still the best
candidate to finish, and failing its writes there would turn a slow run into a
stuck row. Ownership changes when another worker CLAIMS, and that is exactly
when `worker_id` changes.

Four mutations are deliberately NOT fenced, because they exist to act on rows
this worker does not own: `insert_queued`, `coalesce_pr` (the webhook cancelling
someone else's in-flight run), `reap_exhausted` (closing out rows whose worker
is gone) and `set_cancel` (the operator API). `claim` itself establishes
ownership and carries its own predicate.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from core.errors import TransientError

from .db import pool

CLAIM_SQL = """
WITH picked AS (
    -- prior_state travels out with the row: the UPDATE below overwrites state
    -- with 'running', and the worker has to know whether it just claimed fresh
    -- work or a review that is finished and only needs posting.
    SELECT id, state AS prior_state
      FROM runs
     WHERE (state IN ('queued', 'post_pending')
            OR (state = 'running' AND leased_until < now()))  -- dead lease
       AND (not_before IS NULL OR not_before <= now())         -- backoff gate
       AND attempts < %(max_attempts)s                         -- attempt ceiling
     ORDER BY created_at
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE runs r
   SET state        = 'running',
       leased_until = now() + %(lease)s::interval,
       worker_id    = %(worker_id)s,
       attempts     = r.attempts + 1,
       started_at   = COALESCE(r.started_at, now())
  FROM picked
 WHERE r.id = picked.id
RETURNING r.*, picked.prior_state
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def insert_queued(
    *,
    pr_url: str,
    owner: str,
    repo: str,
    pr_number: int,
    dry_run: bool = False,
    installation_id: int | None = None,
    delivery_id: str | None = None,
    head_sha: str | None = None,
) -> str | None:
    """Returns the run id, or None if this delivery was already accepted.

    ON CONFLICT on delivery_id is the first line of defence against GitHub
    redelivery - it rejects the duplicate before any work is scheduled.

    head_sha is written here, not later by the worker. coalesce_pr compares
    against this column; a NULL looks distinct from every SHA, so a smee
    replay or GitHub redelivery would cancel the in-flight run.
    """
    run_id = str(uuid.uuid4())
    with pool().connection() as conn:
        row = conn.execute(
            """
            INSERT INTO runs (id, pr_url, owner, repo, pr_number, dry_run,
                              installation_id, delivery_id, head_sha, state)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING id
            """,
            (run_id, pr_url, owner, repo, pr_number, dry_run,
             installation_id, delivery_id, head_sha),
        ).fetchone()
    return row["id"] if row else None


def coalesce_pr(owner: str, repo: str, pr_number: int, head_sha: str | None) -> dict:
    """Supersede queued work and cancel running work for an older head_sha.

    A branch pushed five times in two minutes must produce one review. Without
    this, cost scales with pushes rather than with pull requests, and the
    author gets five stale comment threads.

    Deliberately UNFENCED. This runs in the API process on a push, and the
    whole job is to act on rows other workers own - cancelling an in-flight
    review is the point. It sets the `cancel` flag rather than a state, so the
    owning worker still decides how its own run ends.
    """
    with pool().connection() as conn:
        superseded = conn.execute(
            """
            UPDATE runs SET state='superseded', finished_at=now()
             WHERE owner=%s AND repo=%s AND pr_number=%s
               AND state='queued'
               AND (head_sha IS DISTINCT FROM %s)
            RETURNING id
            """,
            (owner, repo, pr_number, head_sha),
        ).fetchall()
        cancelled = conn.execute(
            """
            UPDATE runs SET cancel=TRUE
             WHERE owner=%s AND repo=%s AND pr_number=%s
               AND state='running'
               AND (head_sha IS DISTINCT FROM %s)
            RETURNING id
            """,
            (owner, repo, pr_number, head_sha),
        ).fetchall()
    return {"superseded": len(superseded), "cancelled": len(cancelled)}


def claim(*, lease_s: int, worker_id: str, max_attempts: int) -> dict | None:
    """Take the oldest eligible run, or None.

    `attempts` increments HERE, so the row handed back already counts the
    execution about to happen: a fresh row is 0, the first claim returns 1. With
    `max_attempts=3` the predicate `attempts < 3` admits 0, 1 and 2 - three
    executions - and the worker's own `attempts < max_attempts` guard
    dead-letters on the third. The two agree by construction.

    The ceiling is enforced in SQL and not only in the worker because the
    worker's guard lives in an `except` block. A SIGKILL, an OOM kill or a
    container eviction never reaches it: the row stays `running`, its lease
    expires, and before this predicate existed it was claimable again forever -
    an unbounded spend loop on one poison row.
    """
    try:
        with pool().connection() as conn:
            return conn.execute(
                CLAIM_SQL,
                {
                    "lease": timedelta(seconds=lease_s),
                    "worker_id": worker_id,
                    "max_attempts": max_attempts,
                },
            ).fetchone()
    except psycopg.OperationalError as e:
        # A dropped connection - idle pooler close, network blip, DB restart -
        # is transient, not fatal. Surface it as such so the worker backs off
        # and retries instead of crashing the process (which permanently
        # removes a worker, since nothing here restarts it).
        raise TransientError(f"claim: database connection lost: {e}") from None


REAP_SQL = """
UPDATE runs
   SET state        = 'failed',
       error        = %(error)s,
       leased_until = NULL,
       worker_id    = NULL,
       finished_at  = now()
 WHERE state = 'running'
   AND leased_until IS NOT NULL
   AND leased_until < now()
   AND attempts >= %(max_attempts)s
RETURNING id
"""

ATTEMPTS_EXHAUSTED = "attempts exhausted"


def reap_exhausted(*, max_attempts: int) -> list[str]:
    """Dead-letter rows a crashed worker left behind at the attempt ceiling.

    The claim predicate stops such a row being picked up again, which alone
    would leave it `running` forever - invisible to the queue but never
    resolved, and reported as in-flight by any operator view. This closes it
    out explicitly.

    One statement, so the transition is atomic. Two workers may both try; the
    first wins and the second's WHERE no longer matches. There is no external
    side effect to duplicate, so the race is harmless by construction.

    Only expired leases are touched: a row whose worker is alive and
    heartbeating has `leased_until` in the future and is left alone.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            REAP_SQL,
            {"error": ATTEMPTS_EXHAUSTED, "max_attempts": max_attempts},
        ).fetchall()
    return [r["id"] for r in rows]


def _fenced(sql: str, params: dict) -> bool:
    """Run an ownership-fenced UPDATE. True if it matched the row.

    Every fenced statement ends in `RETURNING id`, so "did this apply?" is
    answered by the database rather than inferred. A caller that ignores the
    result is a caller that carries on believing it still owns the run.
    """
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchone() is not None


def heartbeat(run_id: str, *, lease_s: int, worker_id: str) -> bool:
    """Extend the lease of a run still doing work. False if we no longer own it.

    Without this, any review slower than the lease gets reclaimed and reviewed
    twice. The alternative - a lease long enough for the worst case - means a
    crashed worker's job sits stuck for that same worst case.

    Fenced, and this is the one that matters most: an unfenced heartbeat lets a
    stale worker keep EXTENDING a lease that now belongs to someone else, which
    both hides the handover from the new owner and keeps the reaper away. The
    `False` is how the worker learns to stop.
    """
    return _fenced(
        """UPDATE runs SET leased_until = now() + %(lease)s::interval
            WHERE id = %(run_id)s AND worker_id = %(worker_id)s
        RETURNING id""",
        {"lease": timedelta(seconds=lease_s), "run_id": run_id,
         "worker_id": worker_id},
    )


def is_cancelled(run_id: str) -> bool:
    with pool().connection() as conn:
        row = conn.execute("SELECT cancel FROM runs WHERE id=%s", (run_id,)).fetchone()
    return bool(row and row["cancel"])


def set_cancel(run_id: str) -> bool:
    """Deliberately UNFENCED: the operator API cancels runs it does not own.

    Like `coalesce_pr`, this only raises a flag the owning worker reads.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE runs SET cancel=TRUE WHERE id=%s RETURNING id", (run_id,)
        ).fetchone()
    return row is not None


def mark(run_id: str, state: str, *, worker_id: str,
         error: str | None = None) -> bool:
    """Close a run out in a terminal state. False if we no longer own it.

    Degrading through this path records the reason too, so `state='degraded'`
    always carries one however it was reached. Any other state leaves an
    existing reason alone rather than blanking it: a degraded review whose post
    later failed outright is `failed`, and why it was degraded in the first
    place is still worth knowing.
    """
    return _fenced(
        """UPDATE runs
              SET state=%(state)s,
                  error=%(error)s,
                  degraded_reason = CASE WHEN %(state)s = 'degraded'
                                         THEN COALESCE(%(error)s, 'unknown')
                                         ELSE degraded_reason END,
                  finished_at=now()
            WHERE id=%(run_id)s AND worker_id=%(worker_id)s
        RETURNING id""",
        {"state": state, "error": error, "run_id": run_id,
         "worker_id": worker_id},
    )


def requeue(run_id: str, *, error: str, worker_id: str,
            delay_s: float = 0.0) -> bool:
    """Put a run back, optionally not before `delay_s` from now.

    `not_before` is the whole backoff mechanism. Without it this set state to
    'queued' and the next poll - two seconds later - claimed it again, so three
    attempts were spent in about six seconds. Against a rate limit that is a
    retry storm that asks the same question three times and gets the same
    answer.

    Fenced, and it RELEASES ownership by design: after this the row is queued
    with `worker_id` NULL, so this worker's later writes are fenced out too -
    which is correct, because it has handed the run back.
    """
    return _fenced(
        """UPDATE runs SET state='queued', leased_until=NULL,
                           worker_id=NULL, error=%(error)s,
                           not_before = now() + %(delay)s::interval
            WHERE id=%(run_id)s AND worker_id=%(worker_id)s
        RETURNING id""",
        {"error": error, "delay": timedelta(seconds=max(delay_s, 0.0)),
         "run_id": run_id, "worker_id": worker_id},
    )


def requeue_post(run_id: str, *, error: str, worker_id: str,
                 delay_s: float = 0.0) -> bool:
    """Retry only the GitHub post, keeping the computed review.

    State stays `post_pending` and the payload stays in the row, so the next
    claim re-posts instead of re-running the model. This is the difference
    between a failed post costing one API call and costing a whole review.

    Fenced. A `post_pending` row IS owned while it is being posted: `claim`
    sets state='running' and `worker_id` for it exactly as for fresh work, and
    hands the old state back as `prior_state`. So the worker holds a lease
    here, and a stale one must not be able to reset the retry clock.
    """
    return _fenced(
        """UPDATE runs SET state='post_pending', leased_until=NULL,
                           worker_id=NULL, error=%(error)s,
                           not_before = now() + %(delay)s::interval
            WHERE id=%(run_id)s AND worker_id=%(worker_id)s
        RETURNING id""",
        {"error": error, "delay": timedelta(seconds=max(delay_s, 0.0)),
         "run_id": run_id, "worker_id": worker_id},
    )


def mark_posted(run_id: str, *, state: str, posted: bool,
                worker_id: str) -> bool:
    """Close out a run whose post has been settled. False if we lost the lease.

    The findings update is gated on the run UPDATE matching, inside one
    transaction: a stale worker must not be able to flag another worker's
    findings as posted, which is the row that tells an operator a comment
    reached GitHub.

    `error` is cleared here and `degraded_reason` deliberately is not. Clearing
    is right for `error`: it holds the post failure we were retrying, and the
    post has now settled. It was wrong for the degradation reason, which this
    same statement used to erase - leaving `state='degraded'` with nothing
    saying which budget went, the one field that makes the state actionable.
    """
    with pool().connection() as conn, conn.transaction():
        owned = conn.execute(
            """UPDATE runs SET state=%(state)s, error=NULL, not_before=NULL,
                               finished_at=now()
                WHERE id=%(run_id)s AND worker_id=%(worker_id)s
            RETURNING id""",
            {"state": state, "run_id": run_id, "worker_id": worker_id},
        ).fetchone()
        if owned is None:
            return False
        if posted:
            conn.execute(
                "UPDATE findings SET posted=TRUE WHERE run_id=%s AND anchored<>%s",
                (run_id, "dropped"),
            )
    return True


DEGRADED = "degraded"


def degraded_reason_of(result: Any) -> str | None:
    """The durable reason a review was cut short, or None if it was not.

    Keyed on `result.status`, never on the presence of `result.error`: a run
    that FAILED also has an error, and recording that as a degradation reason
    would turn an outright failure into "we published something partial",
    which is a different and much more reassuring claim than the truth.
    """
    if (getattr(result, "status", None) or "") != DEGRADED:
        return None
    return getattr(result, "error", None) or "unknown"


def record_result(run_id: str, result: Any, *, state: str,
                  worker_id: str) -> bool:
    """Commit a computed review. False if another worker owns this run now.

    This is the commit point of the whole pipeline, so it is the most important
    fence: the findings are DELETEd and rewritten here, and the worker posts to
    GitHub immediately afterwards. A stale worker reaching this would replace
    the new owner's result with its own and then post a second review. `False`
    tells the caller to drop the result on the floor and post nothing.

    It is also where `degraded_reason` is written. `result.error` carries the
    reason only while the result is in memory; the row it lands in is the one
    the post path then clears and overwrites, so the reason has to be put
    somewhere with a different lifetime, and this is the only moment it is
    still known.
    """
    g = result.grounding or {}
    a = result.anchoring or {}
    with pool().connection() as conn, conn.transaction():
        owned = conn.execute(
            """
            UPDATE runs SET state=%s, head_sha=%s, prompt_sha=%s, model=%s,
                   tokens_in=%s, tokens_out=%s, wall_clock_s=%s,
                   grounded=%s, near=%s, ungrounded=%s,
                   inline=%s, summary=%s, dropped=%s,
                   corpus=%s, trace=%s, payload=%s,
                   error=%s, degraded_reason=%s, finished_at=now()
             WHERE id=%s AND worker_id=%s
            RETURNING id
            """,
            (state, result.head_sha, result.prompt_sha, result.model,
             result.tokens_in, result.tokens_out, result.wall_clock_s,
             g.get("grounded"), g.get("near"), g.get("ungrounded"),
             a.get("inline"), a.get("summary"), a.get("dropped"),
             json.dumps(result.corpus), json.dumps(result.trace),
             json.dumps(result.payload), result.error,
             degraded_reason_of(result), run_id, worker_id),
        ).fetchone()
        if owned is None:
            return False
        conn.execute("DELETE FROM findings WHERE run_id=%s", (run_id,))
        finding_rows = [
            (str(uuid.uuid4()), run_id, f.severity, f.category, f.path,
             f.line, f.title, f.body, f.evidence, f.verdict, f.anchored,
             f.posted)
            for f in result.findings
        ]
        if finding_rows:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO findings (id, run_id, severity, category, path,
                           line, title, body, evidence, verdict, anchored, posted)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    finding_rows,
                )
    return True


def get_run(run_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute("SELECT * FROM runs WHERE id=%s", (run_id,)).fetchone()


def findings_for(run_id: str) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(
            "SELECT * FROM findings WHERE run_id=%s ORDER BY severity, path, line",
            (run_id,),
        ).fetchall()


def list_runs(*, limit: int = 50, cursor: str | None = None) -> list[dict]:
    with pool().connection() as conn:
        if cursor:
            return conn.execute(
                """SELECT * FROM runs WHERE created_at < %s
                   ORDER BY created_at DESC LIMIT %s""",
                (cursor, limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT %s", (limit,)
        ).fetchall()


def upsert_installation(*, gh_installation_id: int, login: str,
                        account_type: str | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            """INSERT INTO installations (id, gh_installation_id, account_login,
                                          account_type)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (gh_installation_id)
               DO UPDATE SET account_login=EXCLUDED.account_login,
                             suspended=FALSE""",
            (str(uuid.uuid4()), gh_installation_id, login, account_type),
        )


def suspend_installation(gh_installation_id: int) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE installations SET suspended=TRUE WHERE gh_installation_id=%s",
            (gh_installation_id,),
        )
