"""Track GitHub API rate limit headers."""


def remaining(headers: dict) -> int:
    """Requests left in the current window."""
    return int(headers.get("X-RateLimit-Remaining", 0))


def should_wait(headers: dict, threshold: int = 10) -> bool:
    """True when we are close enough to the limit that we should back off."""
    return remaining(headers) < threshold


def reset_in(headers: dict, now: float) -> float:
    """Seconds until the window resets."""
    return float(headers["X-RateLimit-Reset"]) - now
