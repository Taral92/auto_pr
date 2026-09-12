"""What is left, and what the agent is told about it.

One object answers three questions that used to be answered in three places:
is anything exhausted, which dimension went first, and what does the model
read at the end of a tool turn.

Turns are a FUSE, not the work allowance, and that distinction is the whole
point of this module. A turn is not a unit of cost: the wide-refactor run read
six files in ONE turn and took the context from 3,195 to 39,734 tokens - 18% of
the token budget for a tenth of the turn budget. Counting turns to control cost
counts the wrong noun, which is how that run finished inside its ten turns and
outside its token cap, at 205,513 against 200,000, and reported success.

So: tokens, wall clock and tool bytes are the WORK budget - they measure what
the review actually costs. Turns and repeated dead ends are FUSES - they only
catch a loop that has stopped getting anywhere, which no cost dimension can
see, because going nowhere is cheap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: Work dimensions are reported before fuses when several are spent at once.
#: "tokens" tells you what happened; "turns" only tells you it stopped.
WORK = ("tokens", "seconds", "tool_bytes")
FUSES = ("turns", "dead_ends")


@dataclass(frozen=True)
class Dimension:
    name: str
    used: float
    cap: float

    @property
    def fraction(self) -> float:
        return self.used / self.cap if self.cap > 0 else 0.0

    @property
    def spent(self) -> bool:
        return self.cap > 0 and self.used >= self.cap


@dataclass(frozen=True)
class Budget:
    tokens: Dimension
    seconds: Dimension
    tool_bytes: Dimension
    turns: Dimension
    dead_ends: Dimension

    def dimension(self, name: str) -> Dimension:
        return getattr(self, name)

    @property
    def fraction(self) -> float:
        """How spent the run is, by its binding WORK constraint.

        Fuses are excluded deliberately: a generous turn fuse would otherwise
        report a run as 'nearly done' while most of its real budget is intact.
        """
        return max(self.dimension(name).fraction for name in WORK)

    @property
    def breach(self) -> str | None:
        """Which dimension is gone, or None. Work dimensions answer first."""
        for name in (*WORK, *FUSES):
            if self.dimension(name).spent:
                return name
        return None

    def render(self) -> str:
        """The one line the model reads at the end of a tool turn.

        It goes in the message and NEVER in the corpus: grounding matches a
        finding's evidence against the corpus, so a budget note that landed
        there could be quoted back as if it were code under review.
        """
        pct = round(self.fraction * 100)
        facts = (
            f"{_short(self.tokens.used)}/{_short(self.tokens.cap)} tokens, "
            f"{self.seconds.used:.0f}/{self.seconds.cap:.0f}s, "
            f"{_short(self.tool_bytes.used)}/{_short(self.tool_bytes.cap)} tool bytes, "
            f"turn {self.turns.used:.0f}/{self.turns.cap:.0f}"
        )
        return f"[budget {pct}% used - {facts}. {_advice(self.fraction)}]"


def _advice(fraction: float) -> str:
    if fraction >= 0.8:
        return (
            "Finish now: call submit_findings with what you can already "
            "evidence."
        )
    if fraction >= 0.5:
        return (
            "Prefer reading changed files over new searches; "
            "call submit_findings soon."
        )
    return "Call submit_findings as soon as you have enough evidence."


def _short(n: float) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else f"{n:.0f}"


def from_state(state, settings, *, now: float | None = None) -> Budget:
    """Read every dimension off the graph state in one place.

    `now` is injectable so a test can age a run without sleeping through it.
    """
    clock = time.monotonic() if now is None else now
    started = float(state.get("started_at") or clock)
    tokens = int(state.get("tokens_in") or 0) + int(state.get("tokens_out") or 0)
    return Budget(
        tokens=Dimension("tokens", tokens, settings.max_tokens_total),
        seconds=Dimension("seconds", max(clock - started, 0.0), settings.max_wall_clock_s),
        tool_bytes=Dimension(
            "tool_bytes", int(state.get("tool_bytes") or 0), settings.max_tool_bytes_total
        ),
        turns=Dimension("turns", int(state.get("iterations") or 0), settings.max_turns),
        dead_ends=Dimension(
            "dead_ends",
            int(state.get("unproductive_streak") or 0),
            settings.max_unproductive_turns,
        ),
    )
