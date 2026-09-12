"""Webhook signature verification. Fails closed."""

import hashlib
import hmac


def verify(secret: str, body: bytes, header: str) -> bool:
    if not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")
