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
        # What this case is a case OF. Most fixtures assert findings and must
        # finish clean; a budget fixture asserts the governor fires and is only
        # correct when it degrades. Comparing to a per-case contract is what
        # lets "a degraded run is not a pass" hold without making a fixture
        # whose whole point is degrading permanently red.
        c["expect_state"] = c.get("expect_state") or "published"
        # Quality floors, seeded from the measured baseline of each fixture.
        # Defaults are permissive: a fixture without thresholds keeps behaving
        # exactly as before, so adding this gate cannot fail anything on its own.
        for field, default in THRESHOLDS.items():
            c[field] = c.get(field, default)
        if only is None and c.get("enabled") is False:
            continue          # explicit --case still runs a disabled case
        out.append(c)
    return out


#: Threshold field -> permissive default. `max_fp` defaults to None meaning
#: "unbounded", so only a fixture that declares a ceiling gets one.
THRESHOLDS = {
    "min_precision": 0.0,
    "min_recall": 0.0,
    "min_groundedness": 0.0,
    "max_fp": None,
}


def threshold_failures(row: dict) -> list[str]:
    """Every way this row falls short, as strings naming actual vs required.

    The gate used to compare final state and nothing else, which let review
    QUALITY regress silently: wide-refactor could publish four findings for two
    expected ones, and a regression that missed sandbox-escape entirely would
    still have `published` as its state and still exited 0.

    These reuse the metrics `run_case` already computed from `evals/scoring.py`.
    There is deliberately no second scoring path - a gate that measures
    differently from the report it gates is worse than no gate.
    """
    out: list[str] = []
    if row["state"] != row["expect_state"]:
        reason = row.get("error") or row.get("detail") or row["state"]
        out.append(
            f"state {row['state']} != expected {row['expect_state']} "
            f"(after {row['iterations']} iterations: {reason})"
        )
    for metric, field in (
        ("precision", "min_precision"),
        ("recall", "min_recall"),
        ("groundedness", "min_groundedness"),
    ):
        floor = row.get(field)
        if floor is not None and row[metric] < floor:
            out.append(f"{metric} {row[metric]} < required {floor}")
    ceiling = row.get("max_fp")
    if ceiling is not None and row["fp"] > ceiling:
        out.append(f"false positives {row['fp']} > allowed {ceiling}")
    return out


def cassette_name(case: dict, settings) -> str:
    """`<case>.<provider>.<model>` - one recording per model, not per case."""
    explicit = case.get("cassette")
    if explicit:
        return explicit
    return f"{case['id']}.{settings.provider}.{settings.model}"


def run_case(case: dict, *, live: bool, record: bool) -> dict:
    from agent.model_client import ModelClient
    from agent.runtime import model_client_var
    from evals.scoring import Expected, breakeven_precision, net_minutes, score

    mode = "record" if record else ("live" if live else "replay")
    os.environ["MODEL_MODE"] = mode
    os.environ["GITHUB_TOKEN"] = ""
    import config

    config.get_settings.cache_clear()
    settings = config.get_settings()
    # Cassettes are namespaced by provider and model. A recording is a record
    # of how ONE model behaved; replaying GPT's transcript while configured
    # for Claude would silently score the wrong thing, and the whole point of
    # keeping both providers is being able to compare them honestly.
    name = cassette_name(case, settings)
    os.environ["CASSETTE"] = name

    t0 = time.monotonic()
    client = None
    tok = None
    try:
        client = ModelClient(mode=mode, cassette=name)
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
            client.save({
                "case": case["id"],
                "provider": settings.provider,
                "model": settings.model,
                "reasoning_effort": getattr(settings, "reasoning_effort", None),
            })

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
        # A degraded run can still score precision 1.0 - it publishes whatever
        # it had when the budget ran out. Reporting state and iterations next
        # to the scores is what stops a breach from reading as a clean pass.
        "state": (getattr(result, "status", None) if result else None) or "crashed",
        "expect_state": case["expect_state"],
        **{field: case.get(field) for field in THRESHOLDS},
        # Which budget went. `error` here is the graph's, not the harness's -
        # `budget_breach:tokens` says far more than `degraded` does.
        "detail": (getattr(result, "error", None) if result else None),
        "iterations": getattr(result, "iterations", 0) if result else 0,
        "published": s.published,
        "tp": s.tp, "fp": s.fp, "fn": s.fn,
        "precision": round(s.precision, 3),
        "recall": round(s.recall, 3),
        "f1": round(s.f1, 3),
        "groundedness": round(s.groundedness, 3),
        "inline": s.inline,
        "inline_rate": round(s.inline / s.published, 3) if s.published else 0.0,
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
    print(f"{'case':<16}{'state':>11}{'iters':>7}{'pub':>5}{'tp':>4}{'fp':>4}{'fn':>4}"
          f"{'prec':>14}{'recall':>14}{'grounded':>14}"
          f"{'inline_rate':>13}{'net min':>10}")
    print("-" * 120)
    net_total = 0.0
    for cid, rows in by_case.items():
        net = statistics.mean(r["time"]["net_min"] for r in rows)
        net_total += net
        want = rows[0]["expect_state"]
        states = sorted({r["state"] for r in rows})
        shown = "/".join(states) + ("" if states == [want] else f"!={want}")
        print(f"{cid:<16}{shown:>11}{_agg('iterations', rows):>7}"
              f"{_agg('published', rows):>5}{_agg('tp', rows):>4}"
              f"{_agg('fp', rows):>4}{_agg('fn', rows):>4}"
              f"{_agg('precision', rows):>14}{_agg('recall', rows):>14}"
              f"{_agg('groundedness', rows):>14}"
              f"{_agg('inline_rate', rows):>13}{net:>10.1f}")
        for r in rows:
            if r["error"]:
                print(f"             ERROR: {r['error']}")
                break
    print("-" * 120)
    be = max((r["breakeven_precision"] for r in all_rows), default=0)
    print(f"net developer minutes per PR: {net_total / max(len(by_case), 1):+.1f}")
    print(f"break-even precision:         {be:.0%}   "
          f"(below this the agent costs more time than it saves)")

    # A degraded run published what it happened to have when the budget blew.
    # Scoring it as a pass is how iteration exhaustion stayed invisible for as
    # long as it did, so it exits non-zero and says which budget went.
    bad = [(r, threshold_failures(r)) for r in all_rows]
    bad = [(r, why) for r, why in bad if why]
    if bad:
        print()
        for r, why in bad:
            for reason in why:
                print(f"NOT A PASS  {r['case']:<16} {reason}")
        print()
        raise SystemExit(1)
    print()


if __name__ == "__main__":
    main()
