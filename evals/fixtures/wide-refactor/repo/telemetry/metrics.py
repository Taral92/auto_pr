from collections import Counter

_counters: Counter = Counter()


def incr(name: str, by: int = 1) -> None:
    _counters[name] += by


def snapshot() -> dict:
    return dict(_counters)
