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

from .db import pool

CLAIM_SQL = """
WITH picked AS (
    SELECT id
      FROM runs
     WHERE state = 'queued'
        OR (state = 'running' AND leased_until < now())   -- reclaim a dead lease
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
RETURNING r.*
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


def claim(*, lease_s: int, worker_id: str) -> dict | None:
    with pool().connection() as conn:
        return conn.execute(
            CLAIM_SQL,
            {"lease": timedelta(seconds=lease_s), "worker_id": worker_id},
        ).fetchone()


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


def requeue(run_id: str, *, error: str) -> None:
    with pool().connection() as conn:
        conn.execute(
            """UPDATE runs SET state='queued', leased_until=NULL,
                               worker_id=NULL, error=%s WHERE id=%s""",
            (error, run_id),
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
