"""Retry helper for GitHub API calls.

`gh/client.py` raises TransientError on 429 and 5xx. Everything currently
gives up on the first one, so a single rate-limit blip fails a whole review.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

from core.errors import TransientError

T = TypeVar("T")

BASE_DELAY_S = 0.5


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    return random.random() * BASE_DELAY_S * (2 ** attempt)


def with_retry(fn: Callable[[], T], *, max_attempts: int = 3) -> T:
    """Call `fn`, retrying up to `max_attempts` times on failure.

    Sleeps with exponential backoff between attempts. Raises the last
    exception if every attempt fails.
    """
    last: Exception | None = None
    for attempt in range(1, max_attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(_backoff(attempt))
    raise last


def retry_after(headers: dict[str, str]) -> float | None:
    """Seconds to wait, from GitHub's Retry-After header."""
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    return float(raw)
