"""Run queue and history.

The claim query is the whole concurrency story:

    SELECT ... FOR UPDATE SKIP LOCKED

`SKIP LOCKED` is why this scales past one worker. Without it, N workers all
block on the same oldest row and you have a serial queue wearing a pool's
clothes. With it, each worker takes the oldest row nobody else holds.
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


def heartbeat(run_id: str, *, lease_s: int) -> None:
    """Extend the lease of a run still doing work.

    Without this, any review slower than the lease gets reclaimed and reviewed
    twice. The alternative - a lease long enough for the worst case - means a
    crashed worker's job sits stuck for that same worst case.
    """
    with pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET leased_until = now() + %s::interval WHERE id = %s",
            (timedelta(seconds=lease_s), run_id),
        )


def is_cancelled(run_id: str) -> bool:
    with pool().connection() as conn:
        row = conn.execute("SELECT cancel FROM runs WHERE id=%s", (run_id,)).fetchone()
    return bool(row and row["cancel"])


def set_cancel(run_id: str) -> bool:
    with pool().connection() as conn:
        row = conn.execute(
            "UPDATE runs SET cancel=TRUE WHERE id=%s RETURNING id", (run_id,)
        ).fetchone()
    return row is not None


def mark(run_id: str, state: str, *, error: str | None = None) -> None:
    with pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET state=%s, error=%s, finished_at=now() WHERE id=%s",
            (state, error, run_id),
        )


def requeue(run_id: str, *, error: str, delay_s: float = 0.0) -> None:
    """Put a run back, optionally not before `delay_s` from now.

    `not_before` is the whole backoff mechanism. Without it this set state to
    'queued' and the next poll - two seconds later - claimed it again, so three
    attempts were spent in about six seconds. Against a rate limit that is a
    retry storm that asks the same question three times and gets the same
    answer.
    """
    with pool().connection() as conn:
        conn.execute(
            """UPDATE runs SET state='queued', leased_until=NULL,
                               worker_id=NULL, error=%s,
                               not_before = now() + %s::interval
                WHERE id=%s""",
            (error, timedelta(seconds=max(delay_s, 0.0)), run_id),
        )


def requeue_post(run_id: str, *, error: str, delay_s: float = 0.0) -> None:
    """Retry only the GitHub post, keeping the computed review.

    State stays `post_pending` and the payload stays in the row, so the next
    claim re-posts instead of re-running the model. This is the difference
    between a failed post costing one API call and costing a whole review.
    """
    with pool().connection() as conn:
        conn.execute(
            """UPDATE runs SET state='post_pending', leased_until=NULL,
                               worker_id=NULL, error=%s,
                               not_before = now() + %s::interval
                WHERE id=%s""",
            (error, timedelta(seconds=max(delay_s, 0.0)), run_id),
        )


def mark_posted(run_id: str, *, state: str, posted: bool) -> None:
    """Close out a run whose post has been settled."""
    with pool().connection() as conn, conn.transaction():
        conn.execute(
            """UPDATE runs SET state=%s, error=NULL, not_before=NULL,
                               finished_at=now() WHERE id=%s""",
            (state, run_id),
        )
        if posted:
            conn.execute(
                "UPDATE findings SET posted=TRUE WHERE run_id=%s AND anchored<>%s",
                (run_id, "dropped"),
            )


def record_result(run_id: str, result: Any, *, state: str) -> None:
    g = result.grounding or {}
    a = result.anchoring or {}
    with pool().connection() as conn, conn.transaction():
        conn.execute(
            """
            UPDATE runs SET state=%s, head_sha=%s, prompt_sha=%s, model=%s,
                   tokens_in=%s, tokens_out=%s, wall_clock_s=%s,
                   grounded=%s, near=%s, ungrounded=%s,
                   inline=%s, summary=%s, dropped=%s,
                   corpus=%s, trace=%s, payload=%s,
                   error=%s, finished_at=now()
             WHERE id=%s
            """,
            (state, result.head_sha, result.prompt_sha, result.model,
             result.tokens_in, result.tokens_out, result.wall_clock_s,
             g.get("grounded"), g.get("near"), g.get("ungrounded"),
             a.get("inline"), a.get("summary"), a.get("dropped"),
             json.dumps(result.corpus), json.dumps(result.trace),
             json.dumps(result.payload), result.error, run_id),
        )
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
