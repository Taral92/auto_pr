import random


def next_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2)
