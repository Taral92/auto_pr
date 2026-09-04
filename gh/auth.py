"""GitHub App authentication.

    App private key (PEM)
      -> RS256 JWT, max 10 minutes, signed by us
        -> installation access token, 1 hour, per installation
          -> API calls

The one thing that bites in production: installation tokens expire MID-RUN.
A five minute review that starts at minute 58 of a token's life dies halfway.
So nothing holds a token string - callers hold a provider and ask for a token
at the moment they need one.
"""

from __future__ import annotations

import base64
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import certifi
import jwt

from config import get_settings
from core.errors import PermanentError, TransientError

API = "https://api.github.com"
#: Refresh this long before expiry, so a request in flight cannot straddle it.
REFRESH_MARGIN_S = 300


def private_key() -> str:
    """PEM from env. Base64-encoded, because a PEM has newlines and env vars
    that carry raw newlines get mangled by every deploy tool in existence."""
    s = get_settings()
    raw = s.github_app_private_key.get_secret_value()
    if not raw:
        raise PermanentError("GITHUB_APP_PRIVATE_KEY is not set")
    if "BEGIN" in raw:
        return raw
    try:
        return base64.b64decode(raw).decode()
    except Exception as e:
        raise PermanentError(f"GITHUB_APP_PRIVATE_KEY is neither PEM nor base64: {e}")


def app_jwt() -> str:
    s = get_settings()
    nowt = int(time.time())
    return jwt.encode(
        {
            "iat": nowt - 60,        # clock skew: GitHub rejects a future iat
            "exp": nowt + 540,       # 9 min; GitHub's hard ceiling is 10
            "iss": s.github_app_id,
        },
        private_key(),
        algorithm="RS256",
    )


@dataclass
class _Token:
    value: str
    expires_at: float


class InstallationTokens:
    """Per-installation token cache. Thread-safe: the worker pool shares one."""

    def __init__(self) -> None:
        self._cache: dict[int, _Token] = {}
        self._lock = threading.Lock()

    def get(self, installation_id: int) -> str:
        with self._lock:
            tok = self._cache.get(installation_id)
            if tok and tok.expires_at - time.time() > REFRESH_MARGIN_S:
                return tok.value
        fresh = self._mint(installation_id)
        with self._lock:
            self._cache[installation_id] = fresh
        return fresh.value

    def provider(self, installation_id: int):
        """A zero-arg callable. Pass this around instead of a token string, so
        a long run picks up a refreshed token instead of dying on a stale one."""
        return lambda: self.get(installation_id)

    def _mint(self, installation_id: int) -> _Token:
        req = urllib.request.Request(
            f"{API}/app/installations/{installation_id}/access_tokens",
            data=b"",
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {app_jwt()}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "auto-pr")
        try:
            ctx = ssl.create_default_context(cafile=certifi.where())
            with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = f"installation token {e.code}: {e.read().decode()[:400]}"
            if e.code in (429, 500, 502, 503, 504):
                raise TransientError(msg, code=e.code) from None
            raise PermanentError(msg, code=e.code) from None
        except urllib.error.URLError as e:
            raise TransientError(f"installation token failed: {e.reason}") from None
        # GitHub returns ISO8601; parse defensively and fall back to 1h.
        expires = time.time() + 3600
        raw = body.get("expires_at")
        if raw:
            try:
                from datetime import datetime
                expires = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return _Token(body["token"], expires)


TOKENS = InstallationTokens()


def static_provider(token: str):
    """For the CLI, where a PAT is fine and there is nothing to refresh."""
    return lambda: token
