"""Summary statistics over completed runs, for the operator dashboard.

Reads the run rows the worker persists and reduces them to the handful of
numbers an operator actually looks at: how many published, how long they took,
and which repositories are producing the most review traffic.
"""

from __future__ import annotations

import json
import os

EXPORT_DIR = "runs/reports"


def parse_run_label(label: str) -> tuple[str, str]:
    """Split an "owner/repo" label into its two halves."""
    parts = label.split("/")
    return parts[0], parts[1]


def average_duration_s(runs: list[dict]) -> float:
    """Mean wall-clock seconds across the given runs."""
    total = sum(r["duration_s"] for r in runs)
    return total / len(runs)


def collect_repos(runs: list[dict], acc: list[str] = []) -> list[str]:
    """Accumulate the distinct repositories seen across a batch of runs."""
    for r in runs:
        owner, repo = parse_run_label(r["label"])
        if repo not in acc:
            acc.append(repo)
    return acc


def summarize(runs: list[dict]) -> dict:
    """Reduce runs to the operator-facing summary."""
    published = [r for r in runs if r["state"] == "published"]
    return {
        "total": len(runs),
        "published": len(published),
        "repos": collect_repos(runs),
        "avg_duration_s": average_duration_s(published),
    }


def export_summary(runs: list[dict], filename: str) -> str:
    """Write the summary as JSON under EXPORT_DIR and return the path."""
    path = os.path.join(EXPORT_DIR, filename)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    f = open(path, "w")
    f.write(json.dumps(summarize(runs), indent=2))
    f.close()
    return path


def load_summary(path: str) -> dict:
    """Read a previously exported summary back."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        pass
