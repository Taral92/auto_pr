"""How long to wait before retrying a transient failure.

`TransientError` has always been documented as "retry with backoff", but
nothing ever backed off: the worker set a run straight back to `queued` and the
next poll two seconds later picked it up again. Three attempts were spent in
about six seconds, which is not a retry policy - against a rate limit it is a
retry storm that guarantees the same answer three times.

Two rules, in order:

1. If the service told us when to come back, do that. It is the only party that
   knows when its limit resets.
2. Otherwise grow the wait exponentially from a small base, with jitter, so a
   fleet of workers that all tripped the same limit do not come back in step.

Both are CAPPED. An unbounded `Retry-After` from a misbehaving proxy would park
a run for hours; a capped wait re-queues it and lets the lease machinery decide.
"""

from __future__ import annotations

import random

#: First retry waits about this long.
BASE_DELAY_S = 5.0
#: Nothing ever waits longer than this, whoever asked.
MAX_DELAY_S = 300.0
#: Jitter as a fraction of the computed delay: +/- 25%.
JITTER = 0.25


def delay_for(
    attempt: int,
    *,
    retry_after: float | None = None,
    rand: random.Random | None = None,
) -> float:
    """Seconds to wait before retry number `attempt` (1 = the first retry).

    `retry_after` is honoured as-is (clamped), without jitter - the service gave
    a time, and smearing it would only push some workers back inside the window.
    """
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), MAX_DELAY_S)
    step = BASE_DELAY_S * (2 ** max(attempt - 1, 0))
    capped = min(step, MAX_DELAY_S)
    spread = capped * JITTER
    picker = rand or random
    return max(0.0, min(capped + picker.uniform(-spread, spread), MAX_DELAY_S))
