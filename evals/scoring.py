"""Scoring a review run against known ground truth.

Two things are measured, and they are not the same:

1. Is the agent CORRECT?          precision / recall / groundedness / anchor rate
2. Does it SAVE DEVELOPER TIME?   see net_minutes() - a different question with
                                  a different answer, and precision dominates it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Expected:
    """One known defect. `must_contain` anchors it to real code, not a title.

    Matching on titles is hopeless - the model phrases them differently every
    run. Matching on (path, a substring the evidence or body must contain)
    is stable across runs and across prompt versions.
    """

    id: str
    path: str
    category: str
    must_contain: list[str]
    severity: str | None = None


@dataclass
class Match:
    expected_id: str | None
    finding_index: int | None
    kind: str  # tp | fp | fn


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    grounded: int = 0
    near: int = 0
    ungrounded: int = 0
    inline: int = 0
    matches: list[Match] = field(default_factory=list)

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def published(self) -> int:
        return self.tp + self.fp

    @property
    def groundedness(self) -> float:
        total = self.grounded + self.near + self.ungrounded
        return self.grounded / total if total else 0.0


def _hits(finding: dict, exp: Expected) -> bool:
    if (finding.get("path") or finding.get("file") or "") != exp.path:
        return False
    if exp.category and finding.get("category") != exp.category:
        return False
    hay = " ".join(
        str(finding.get(k) or "")
        for k in ("evidence", "title", "body", "description", "recommendation")
    )
    return all(needle in hay for needle in exp.must_contain)


def score(findings: list[dict], expected: list[Expected]) -> Score:
    """Only findings that would actually reach a human are scored.

    A finding the gate dropped never reaches the PR, so it is neither a true
    nor a false positive - it costs nobody any time. Counting dropped findings
    as false positives would punish the gate for doing its job.
    """
    s = Score()
    for f in findings:
        v = f.get("verdict")
        if v in ("grounded", "near", "ungrounded"):
            setattr(s, v, getattr(s, v) + 1)
        if f.get("anchored") == "inline":
            s.inline += 1

    visible = [
        (i, f) for i, f in enumerate(findings) if f.get("anchored") in ("inline", "summary")
    ]
    claimed: set[int] = set()

    for exp in expected:
        hit = next((i for i, f in visible if i not in claimed and _hits(f, exp)), None)
        if hit is None:
            s.fn += 1
            s.matches.append(Match(exp.id, None, "fn"))
        else:
            claimed.add(hit)
            s.tp += 1
            s.matches.append(Match(exp.id, hit, "tp"))

    for i, _ in visible:
        if i not in claimed:
            s.fp += 1
            s.matches.append(Match(None, i, "fp"))
    return s


# --- the question the project actually exists to answer ------------------

#: Minutes a human spends finding this defect unaided, by severity. Rough, and
#: deliberately conservative - inflating these makes any agent look good.
FIND_COST_MIN = {"blocker": 12.0, "should_fix": 5.0, "nit": 1.0}

#: Minutes a reviewer burns reading a wrong comment and deciding to dismiss it.
#: This is the tax, and it is charged on EVERY false positive.
DISMISS_COST_MIN = 1.5


def net_minutes(
    s: Score,
    expected: list[Expected],
    dismiss_cost: float = DISMISS_COST_MIN,
) -> dict:
    """Net developer minutes saved per PR.

        saved   = sum(find_cost of each true positive)
        wasted  = false positives x dismiss cost
        net     = saved - wasted

    Missed defects (fn) are scored 0, not negative: without the agent the
    developer was going to miss them too. The agent is not made worse by
    failing to help.

    The consequence worth internalising: at low precision the agent COSTS
    time. Twenty findings with two right is -27 minutes per PR, and a team
    turns it off in a week. Precision matters more than recall here.
    """
    by_id = {e.id: e for e in expected}
    saved = 0.0
    for m in s.matches:
        if m.kind == "tp" and m.expected_id in by_id:
            sev = by_id[m.expected_id].severity or "should_fix"
            saved += FIND_COST_MIN.get(sev, 5.0)
    wasted = s.fp * dismiss_cost
    return {
        "saved_min": round(saved, 2),
        "wasted_min": round(wasted, 2),
        "net_min": round(saved - wasted, 2),
        "verdict": (
            "saves time" if saved > wasted
            else "costs time" if wasted > saved
            else "neutral"
        ),
    }


def breakeven_precision(expected: list[Expected], dismiss_cost: float = DISMISS_COST_MIN) -> float:
    """Precision below which the agent is a net time sink.

    Derivation: with precision p, publishing n findings gives p*n true
    positives worth `avg_find` each and (1-p)*n false positives costing
    `dismiss_cost` each. Break-even is p*avg_find = (1-p)*dismiss_cost.
    """
    if not expected:
        return 0.0
    avg_find = sum(
        FIND_COST_MIN.get(e.severity or "should_fix", 5.0) for e in expected
    ) / len(expected)
    return round(dismiss_cost / (avg_find + dismiss_cost), 3)
