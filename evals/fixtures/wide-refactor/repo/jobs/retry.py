from core.errors import TransientError


def should_retry(exc: BaseException, attempts: int, limit: int = 3) -> bool:
    return isinstance(exc, TransientError) and attempts < limit
