"""Webhook signature verification.

Constant-time compare, always. A `==` here leaks the secret one byte at a
time to anyone willing to measure response latency.
"""

from __future__ import annotations

import hashlib
import hmac

HEADER = "X-Hub-Signature-256"


def verify(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def should_review(event: str, payload: dict) -> bool:
    if event != "pull_request":
        return False
    if payload.get("action") not in {"opened", "synchronize", "reopened"}:
        return False
    pr = payload.get("pull_request") or {}
    if pr.get("draft"):
        return False
    # Never review our own output, or another bot's noise.
    if (pr.get("user") or {}).get("type") == "Bot":
        return False
    return True
