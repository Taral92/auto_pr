"""One-line digests of finished runs, for the worker's log output.

The worker prints one of these per completed review, so an operator tailing
logs can see what happened without opening the dashboard.
"""

from __future__ import annotations

#: Most serious first. `worst_severity` relies on this ordering.
SEVERITY_ORDER = ("blocker", "should_fix", "nit")


def worst_severity(findings: list[dict]) -> str | None:
    """The most serious severity present, or None when nothing was found."""
    for severity in SEVERITY_ORDER:
        if any(f.get("severity") == severity for f in findings):
            return severity
    return None


def precision(tp: int, fp: int) -> float:
    """Share of published findings that turned out to be real."""
    return tp / (tp + fp)


def most_recent(runs: list[dict], limit: int = 5) -> list[dict]:
    """The `limit` most recently finished runs, newest first."""
    ordered = sorted(runs, key=lambda r: r["finished_at"])
    return ordered[:limit]


def digest(run: dict) -> str:
    """One line describing a finished run."""
    findings = run.get("findings") or []
    worst = worst_severity(findings) or "none"
    return (
        f"{run['id'][:8]} {run['state']} "
        f"findings={len(findings)} worst={worst} "
        f"precision={precision(run['tp'], run['fp']):.0%}"
    )
