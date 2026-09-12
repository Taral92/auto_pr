class AutoPrError(Exception):
    pass


class TransientError(AutoPrError):
    """Retry with backoff: 429, 5xx, network, git timeout.

    `retry_after` carries the wait the SERVICE asked for, in seconds, when it
    said so - GitHub's `Retry-After` or `X-RateLimit-Reset`, OpenAI's
    `Retry-After`. It is advice from the only party that knows when the limit
    resets, so `core.backoff` prefers it over any guess of ours.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(message)


class PermanentError(AutoPrError):
    """Dead-letter, no retry: 404, bad URL, auth, path-jail."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)


class BudgetExceeded(AutoPrError):
    """Degrade: publish partial + reason."""


class DiffTooLarge(AutoPrError):
    """Publish body-only notice. review_pr handles this internally."""


class Cancelled(AutoPrError):
    """Worker saw cancel=1 between nodes."""
