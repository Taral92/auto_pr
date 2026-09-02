import json
import sqlite3
import time
import uuid
from typing import Any

from core.models import ReviewResult

CLAIM_SQL = """
UPDATE runs
   SET state='running', leased_until=:leased_until, worker_id=:wid,
       attempts=attempts+1, started_at=:now
 WHERE id = (
       SELECT id FROM runs
        WHERE state='queued'
           OR (state='running' AND leased_until < :now)
        ORDER BY created_at
        LIMIT 1)
RETURNING id
"""


def insert_queued(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    pr_url: str,
    owner: str,
    repo: str,
    pr_number: int,
    dry_run: bool,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (
            id, pr_url, owner, repo, pr_number, dry_run, state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (run_id, pr_url, owner, repo, pr_number, int(dry_run), time.time()),
    )
    conn.commit()


def claim(
    conn: sqlite3.Connection, *, now: float, lease_s: float, worker_id: str
) -> sqlite3.Row | None:
    cur = conn.execute(
        CLAIM_SQL,
        {"leased_until": now + lease_s, "wid": worker_id, "now": now},
    )
    row = cur.fetchone()
    if row is None:
        conn.commit()
        return None
    full = conn.execute("SELECT * FROM runs WHERE id = ?", (row["id"],)).fetchone()
    conn.commit()
    return full


def get_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()


def list_runs(
    conn: sqlite3.Connection, *, limit: int, cursor: float | None
) -> list[sqlite3.Row]:
    if cursor is None:
        return list(
            conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        )
    return list(
        conn.execute(
            """
            SELECT * FROM runs
             WHERE created_at < ?
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (cursor, limit),
        )
    )


def findings_for(conn: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        )
    )


def set_cancel(conn: sqlite3.Connection, run_id: str) -> bool:
    cur = conn.execute("UPDATE runs SET cancel = 1 WHERE id = ?", (run_id,))
    conn.commit()
    return cur.rowcount > 0


def is_cancelled(conn: sqlite3.Connection, run_id: str) -> bool:
    row = conn.execute("SELECT cancel FROM runs WHERE id = ?", (run_id,)).fetchone()
    return bool(row and row["cancel"])


def requeue(conn: sqlite3.Connection, run_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE runs
           SET state='queued', leased_until=NULL, worker_id=NULL, error=?
         WHERE id=?
        """,
        (error, run_id),
    )
    conn.commit()


def mark_state(
    conn: sqlite3.Connection,
    run_id: str,
    state: str,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs
           SET state=?, error=?, finished_at=?
         WHERE id=?
        """,
        (state, error, time.time(), run_id),
    )
    conn.commit()


def record_result(
    conn: sqlite3.Connection, run_id: str, result: ReviewResult, state: str
) -> None:
    g = result.grounding
    a = result.anchoring
    conn.execute(
        """
        UPDATE runs SET
            head_sha=?,
            state=?,
            error=?,
            prompt_sha=?,
            model=?,
            tokens_in=?,
            tokens_out=?,
            wall_clock_s=?,
            grounded=?, near=?, ungrounded=?,
            inline=?, summary=?, dropped=?,
            corpus_json=?,
            trace_json=?,
            payload_json=?,
            finished_at=?
         WHERE id=?
        """,
        (
            result.head_sha,
            state,
            result.error,
            result.prompt_sha,
            result.model,
            result.tokens_in,
            result.tokens_out,
            result.wall_clock_s,
            g.get("grounded"),
            g.get("near"),
            g.get("ungrounded"),
            a.get("inline"),
            a.get("summary"),
            a.get("dropped"),
            json.dumps(result.corpus),
            json.dumps(result.trace),
            json.dumps(result.payload),
            time.time(),
            run_id,
        ),
    )
    conn.execute("DELETE FROM findings WHERE run_id = ?", (run_id,))
    for f in result.findings:
        conn.execute(
            """
            INSERT INTO findings (
                id, run_id, severity, category, path, line, title, body,
                evidence, verdict, anchored, posted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                run_id,
                f.severity,
                f.category,
                f.path,
                f.line,
                f.title,
                f.body,
                f.evidence,
                f.verdict,
                f.anchored,
                int(f.posted),
            ),
        )
    conn.commit()


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM runs GROUP BY state"
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}
