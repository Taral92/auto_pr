"""Run eval cases and report.

    python -m evals.runner                      # all cases, replay, 1 rep
    python -m evals.runner --reps 5             # variance across runs
    python -m evals.runner --live --record      # spend tokens, save cassettes
    python -m evals.runner --case sandbox-escape

Replay is the default on purpose. An eval you cannot afford to run is an eval
you will not run.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

from config import ROOT

FIXTURES = ROOT / "evals" / "fixtures"


def load_cases(only: str | None) -> list[dict]:
    out = []
    for fixture in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        if only is not None and only != fixture.name:
            continue
        c = json.loads((fixture / "expected.json").read_text())
        c["id"] = fixture.name
        c["repo_dir"] = str(fixture / "repo")
        c["diff"] = (fixture / "head.diff").read_text()
        if only is None and c.get("enabled") is False:
            continue          # explicit --case still runs a disabled case
        out.append(c)
    return out


def run_case(case: dict, *, live: bool, record: bool) -> dict:
    from agent.model_client import ModelClient
    from agent.runtime import model_client_var
    from evals.scoring import Expected, breakeven_precision, net_minutes, score

    mode = "record" if record else ("live" if live else "replay")
    os.environ["MODEL_MODE"] = mode
    os.environ["CASSETTE"] = case.get("cassette") or case["id"]
    os.environ["GITHUB_TOKEN"] = ""
    if mode == "replay":
        os.environ["ANTHROPIC_API_KEY"] = ""
    import config

    config.get_settings.cache_clear()

    t0 = time.monotonic()
    client = None
    tok = None
    try:
        client = ModelClient(mode=mode, cassette=case.get("cassette") or case["id"])
        tok = model_client_var.set(client)
        from agent.local import review_local

        result = review_local(
            case["repo_dir"],
            case["diff"],
            run_id=case["id"],
        )
        findings = [f.model_dump() for f in result.findings]
        err = None
    except Exception as e:  # a crashed case is a result, not a stack trace
        findings, err = [], f"{type(e).__name__}: {e}"
        result = None
    finally:
        if tok is not None:
            model_client_var.reset(tok)
        # BUG 2: only save a cassette for a run that actually completed. A
        # cassette recorded from a crashed run replays the crash forever.
        if record and client is not None and err is None:
            client.save({"case": case["id"]})

    if err and "cassette not found" in err:
        err = "no cassette - run with --record first"

    expected = [Expected(**e) for e in case["expected"]]
    s = score(findings, expected)
    return {
        "case": case["id"],
        "error": err,
        "elapsed_s": round(time.monotonic() - t0, 2),
        "tokens_in": getattr(result, "tokens_in", 0) if result else 0,
        "tokens_out": getattr(result, "tokens_out", 0) if result else 0,
        "published": s.published,
        "tp": s.tp, "fp": s.fp, "fn": s.fn,
        "precision": round(s.precision, 3),
        "recall": round(s.recall, 3),
        "f1": round(s.f1, 3),
        "groundedness": round(s.groundedness, 3),
        "inline": s.inline,
        "time": net_minutes(s, expected),
        "breakeven_precision": breakeven_precision(expected),
    }


def _agg(key: str, rows: list[dict]):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return "-"
    if len(vals) == 1:
        return f"{vals[0]:g}"
    return f"{statistics.mean(vals):.2f} ±{statistics.pstdev(vals):.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m evals.runner")
    ap.add_argument("--case")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--live", action="store_true", help="call the API")
    ap.add_argument("--record", action="store_true", help="call the API and save cassettes")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cases = load_cases(a.case)
    if not cases:
        raise SystemExit("no cases matched")

    all_rows: list[dict] = []
    for case in cases:
        rows = [run_case(case, live=a.live, record=a.record) for _ in range(a.reps)]
        all_rows.extend(rows)

    if a.json:
        print(json.dumps(all_rows, indent=2))
        return

    by_case: dict[str, list[dict]] = {}
    for r in all_rows:
        by_case.setdefault(r["case"], []).append(r)

    print()
    print(f"{'case':<12}{'pub':>5}{'tp':>4}{'fp':>4}{'fn':>4}"
          f"{'prec':>14}{'recall':>14}{'grounded':>14}{'net min':>10}")
    print("-" * 81)
    net_total = 0.0
    for cid, rows in by_case.items():
        net = statistics.mean(r["time"]["net_min"] for r in rows)
        net_total += net
        print(f"{cid:<12}{_agg('published', rows):>5}{_agg('tp', rows):>4}"
              f"{_agg('fp', rows):>4}{_agg('fn', rows):>4}"
              f"{_agg('precision', rows):>14}{_agg('recall', rows):>14}"
              f"{_agg('groundedness', rows):>14}{net:>10.1f}")
        for r in rows:
            if r["error"]:
                print(f"             ERROR: {r['error']}")
                break
    print("-" * 81)
    be = max((r["breakeven_precision"] for r in all_rows), default=0)
    print(f"net developer minutes per PR: {net_total / max(len(by_case), 1):+.1f}")
    print(f"break-even precision:         {be:.0%}   "
          f"(below this the agent costs more time than it saves)")
    print()


if __name__ == "__main__":
    main()
