import time


def now() -> float:
    return time.time()


def monotonic() -> float:
    return time.monotonic()
