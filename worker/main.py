import logging
import os
import socket
import time

from config import get_settings
from core.errors import BudgetExceeded, Cancelled, PermanentError, TransientError
from agent.review import review_pr
from agent.runtime import is_cancelled, run_id_var
from storage.db import connect, init_db
from storage.runs import (
    claim,
    is_cancelled as db_cancelled,
    mark_state,
    record_result,
    requeue,
)

log = logging.getLogger("worker")
MAX_ATTEMPTS = 3


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _secret_token() -> str:
    return get_settings().github_token.get_secret_value()


def run_loop() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    lease_s = settings.max_wall_clock_s + 60
    wid = _worker_id()
    conn = connect()
    init_db(conn)
    log.info("worker %s starting lease_s=%s", wid, lease_s)
    while True:
        row = claim(conn, now=time.time(), lease_s=lease_s, worker_id=wid)
        if row is None:
            time.sleep(2)
            continue
        run_id = row["id"]
        log.info("claimed %s attempts=%s", run_id, row["attempts"])
        rid_tok = run_id_var.set(run_id)

        def _cancel_check() -> bool:
            c = connect()
            try:
                return db_cancelled(c, run_id)
            finally:
                c.close()

        cancel_tok = is_cancelled.set(_cancel_check)
        try:
            result = review_pr(
                row["owner"],
                row["repo"],
                row["pr_number"],
                _secret_token(),
                dry_run=bool(row["dry_run"]),
            )
            state = result.status or "published"
            if state == "too_large":
                state = "published"
            if state not in ("published", "degraded", "failed"):
                state = "published"
            record_result(conn, run_id, result, state)
            log.info("finished %s state=%s", run_id, state)
        except TransientError as e:
            if row["attempts"] < MAX_ATTEMPTS:
                log.warning("transient %s: %s; requeue", run_id, e)
                requeue(conn, run_id, str(e))
            else:
                log.error("transient exhausted %s: %s", run_id, e)
                mark_state(conn, run_id, "failed", error=str(e))
        except PermanentError as e:
            log.error("permanent %s: %s", run_id, e)
            mark_state(conn, run_id, "failed", error=str(e))
        except BudgetExceeded as e:
            log.warning("budget %s: %s", run_id, e)
            mark_state(conn, run_id, "degraded", error=str(e))
        except Cancelled as e:
            log.info("cancelled %s", run_id)
            mark_state(conn, run_id, "cancelled", error=str(e))
        except Exception:
            log.exception("bug: unexpected error on %s", run_id)
            mark_state(conn, run_id, "failed", error="unexpected error")
        finally:
            run_id_var.reset(rid_tok)
            is_cancelled.reset(cancel_tok)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
