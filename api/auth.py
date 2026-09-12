"""Operator API authentication.

The webhook is public by necessity: GitHub has to reach it, and it
authenticates itself per request with an HMAC over the body (`gh/webhook.py`).
Everything under `/api` is a different matter. `/api/runs/{id}/trace` returns
the corpus — the diff plus every tool result, which is the source of a private
repository — and `/api/review` enqueues model work against the operator's own
GitHub token. Neither can be open to whoever finds the port, and in the
documented deployment that port is the same one GitHub talks to.

Fail closed, unconditionally. With no `OPERATOR_SECRET` configured the operator
API answers 503 instead of running unauthenticated. There is deliberately no
"development mode" that opens it: an environment flag that disables auth is
exactly the flag that gets left on in production.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from config import get_settings

UNCONFIGURED = "operator API is disabled: OPERATOR_SECRET is not configured"
REJECTED = "invalid operator credentials"


def _presented_matches(header: str | None, secret: str) -> bool:
    """Constant-time compare of a `Bearer <secret>` header against the secret.

    Compared as bytes, not str: `hmac.compare_digest` raises on str containing
    non-ASCII, and the header is attacker-controlled, so a 500 would otherwise
    be one unicode character away.
    """
    if not header:
        return False
    scheme, _, presented = header.partition(" ")
    presented = presented.strip()
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))


def require_operator(authorization: str | None = Header(default=None)) -> None:
    """Gate every `/api` route. Raises; returns nothing on success.

    The failure messages say only that a check failed. They never echo the
    presented credential or any part of the configured one - an error body is
    the easiest place in a service to leak a secret into a log aggregator.
    """
    secret = get_settings().operator_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail=UNCONFIGURED)
    if not _presented_matches(authorization, secret):
        raise HTTPException(
            status_code=401,
            detail=REJECTED,
            headers={"WWW-Authenticate": "Bearer"},
        )
