"""Claim one queued row without serialising the workers."""

CLAIM_SQL = """
UPDATE runs SET status = 'running', lease_until = now() + %s
WHERE id = (
    SELECT id FROM runs
    WHERE status = 'queued' AND run_after <= now()
    ORDER BY run_after
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING id, payload
"""
