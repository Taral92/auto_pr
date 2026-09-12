from typing import Callable

_is_cancelled: Callable[[], bool] = lambda: False


def install(predicate: Callable[[], bool]) -> None:
    global _is_cancelled
    _is_cancelled = predicate


def check() -> None:
    if _is_cancelled():
        raise RuntimeError("cancelled")
