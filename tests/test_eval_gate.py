"""The replay-eval quality gate.

Before this, the gate compared final state and nothing else. wide-refactor could
publish four findings for two expected ones and CI exited 0; a regression that
missed sandbox-escape entirely would still have `published` as its state and
still passed. The point of these tests is not to restate the current numbers -
it is to prove that a DELIBERATELY DEGRADED metric makes the CI command fail.

The gate is tested through `threshold_failures` (pure, no model) and through
`main()` itself (proving the exit code), so no live or replay model call is
needed for the regression cases.
"""

import json

import pytest

from evals.runner import FIXTURES, THRESHOLDS, load_cases, threshold_failures


def _row(**over) -> dict:
    """A passing row, shaped exactly as run_case returns one."""
    base = {
        "case": "fixture", "state": "published", "expect_state": "published",
        "iterations": 1, "error": None, "detail": None,
        "tp": 2, "fp": 0, "fn": 0,
        "precision": 1.0, "recall": 1.0, "groundedness": 1.0,
        "min_precision": 1.0, "min_recall": 1.0, "min_groundedness": 1.0,
        "max_fp": 0,
        # The rest of what run_case returns; main() renders these.
        "published": 2, "f1": 1.0, "inline": 2, "inline_rate": 1.0,
        "breakeven_precision": 0.111, "tokens_in": 100, "tokens_out": 10,
        "elapsed_s": 0.1,
        "time": {"saved_min": 24.0, "wasted_min": 0.0, "net_min": 24.0,
                 "verdict": "saves time"},
    }
    return {**base, **over}


# -- the baseline passes ---------------------------------------------------


def test_a_baseline_row_has_no_failures():
    assert threshold_failures(_row()) == []


@pytest.mark.parametrize("name,metrics", [
    ("no-defect", {"tp": 0, "fp": 0, "fn": 0, "precision": 0.0,
                   "recall": 0.0, "groundedness": 0.0}),
    ("sandbox-escape", {"tp": 2, "fp": 0, "fn": 0, "precision": 1.0,
                        "recall": 1.0, "groundedness": 1.0}),
    ("wide-refactor", {"tp": 2, "fp": 2, "fn": 0, "precision": 0.5,
                       "recall": 1.0, "groundedness": 1.0}),
])
def test_every_current_baseline_passes_its_own_thresholds(name, metrics):
    """The thresholds in expected.json must admit the measured baseline."""
    case = next(c for c in load_cases(name) if c["id"] == name)
    row = _row(case=name, **metrics,
               **{f: case[f] for f in THRESHOLDS})
    assert threshold_failures(row) == []


# -- each regression is caught -------------------------------------------


def test_sandbox_escape_recall_dropping_to_zero_fails():
    """A regression that finds neither planted security defect."""
    case = next(c for c in load_cases("sandbox-escape"))
    row = _row(case="sandbox-escape", tp=0, fn=2, precision=0.0, recall=0.0,
               groundedness=0.0, **{f: case[f] for f in THRESHOLDS})
    why = threshold_failures(row)
    assert any("recall 0.0 < required 1.0" in w for w in why), why


def test_no_defect_producing_a_false_positive_fails():
    """Silence on correct code is the entire point of that fixture."""
    case = next(c for c in load_cases("no-defect"))
    row = _row(case="no-defect", tp=0, fp=1, fn=0, precision=0.0, recall=0.0,
               groundedness=1.0, **{f: case[f] for f in THRESHOLDS})
    why = threshold_failures(row)
    assert any("false positives 1 > allowed 0" in w for w in why), why


def test_wide_refactor_precision_below_baseline_fails():
    case = next(c for c in load_cases("wide-refactor"))
    row = _row(case="wide-refactor", tp=2, fp=3, precision=0.4, recall=1.0,
               groundedness=1.0, **{f: case[f] for f in THRESHOLDS})
    why = threshold_failures(row)
    assert any("precision 0.4 < required 0.5" in w for w in why), why
    assert any("false positives 3 > allowed 2" in w for w in why), why


def test_a_groundedness_regression_fails():
    """Findings published on paraphrased evidence."""
    why = threshold_failures(_row(groundedness=0.5))
    assert any("groundedness 0.5 < required 1.0" in w for w in why), why


def test_excessive_false_positives_fail():
    why = threshold_failures(_row(fp=5, precision=1.0))
    assert any("false positives 5 > allowed 0" in w for w in why), why


def test_a_state_mismatch_still_fails():
    why = threshold_failures(_row(state="degraded", detail="budget_breach:tokens"))
    assert any("state degraded != expected published" in w for w in why), why
    assert any("budget_breach:tokens" in w for w in why), why


def test_a_crashed_run_still_fails():
    why = threshold_failures(
        _row(state="crashed", error="ReplayExhausted", precision=0.0, recall=0.0,
             groundedness=0.0)
    )
    assert why and any("crashed" in w for w in why)


def test_several_failures_are_all_reported():
    """The output must name every threshold that failed, not just the first."""
    why = threshold_failures(
        _row(state="degraded", precision=0.1, recall=0.2, groundedness=0.3, fp=9)
    )
    assert len(why) == 5


def test_a_failure_message_states_actual_and_required():
    why = threshold_failures(_row(precision=0.25))
    assert why == ["precision 0.25 < required 1.0"]


# -- exactly at the threshold is a pass ----------------------------------


@pytest.mark.parametrize("field,metric,value", [
    ("min_precision", "precision", 1.0),
    ("min_recall", "recall", 1.0),
    ("min_groundedness", "groundedness", 1.0),
])
def test_exactly_meeting_a_floor_passes(field, metric, value):
    assert threshold_failures(_row(**{metric: value, field: value})) == []


def test_fp_exactly_at_the_ceiling_passes():
    assert threshold_failures(
        _row(fp=2, max_fp=2, precision=0.5, min_precision=0.5)
    ) == []


def test_absent_thresholds_are_permissive():
    """A fixture with no thresholds must behave exactly as before."""
    row = _row(precision=0.0, recall=0.0, groundedness=0.0, fp=99,
               min_precision=0.0, min_recall=0.0, min_groundedness=0.0,
               max_fp=None)
    assert threshold_failures(row) == []


# -- the CI command itself exits non-zero -------------------------------


def _run_main(monkeypatch, rows):
    """Drive evals.runner.main() with canned rows - no model, no cassettes."""
    import evals.runner as R

    monkeypatch.setattr(R, "run_case", lambda case, **kw: rows.pop(0))
    monkeypatch.setattr("sys.argv", ["evals.runner", "--case", "sandbox-escape"])
    return R.main


def test_the_ci_command_exits_zero_when_thresholds_are_met(monkeypatch, capsys):
    case = next(c for c in load_cases("sandbox-escape"))
    row = _row(case="sandbox-escape", **{f: case[f] for f in THRESHOLDS})
    main = _run_main(monkeypatch, [row])
    main()                                    # no SystemExit
    assert "NOT A PASS" not in capsys.readouterr().out


@pytest.mark.parametrize("degraded,expected_text", [
    ({"recall": 0.0, "tp": 0, "fn": 2}, "recall 0.0 < required 1.0"),
    ({"precision": 0.4, "fp": 3}, "precision 0.4 < required 1.0"),
    ({"groundedness": 0.0}, "groundedness 0.0 < required 1.0"),
    ({"fp": 7}, "false positives 7 > allowed 0"),
    ({"state": "degraded"}, "state degraded != expected published"),
])
def test_the_ci_command_exits_non_zero_for_each_regression(
    monkeypatch, capsys, degraded, expected_text
):
    """This is the requirement: a degraded metric must fail the CI step."""
    case = next(c for c in load_cases("sandbox-escape"))
    row = _row(case="sandbox-escape", **{f: case[f] for f in THRESHOLDS},
               **degraded)
    main = _run_main(monkeypatch, [row])

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "NOT A PASS" in out
    assert expected_text in out


# -- the fixtures themselves carry thresholds ---------------------------


@pytest.mark.parametrize("name", ["no-defect", "sandbox-escape", "wide-refactor"])
def test_every_fixture_declares_its_thresholds(name):
    """An un-annotated fixture would be silently ungated."""
    raw = json.loads((FIXTURES / name / "expected.json").read_text())
    for field in THRESHOLDS:
        assert field in raw, f"{name} is missing {field}"


def test_no_defect_forbids_false_positives():
    raw = json.loads((FIXTURES / "no-defect" / "expected.json").read_text())
    assert raw["max_fp"] == 0


def test_sandbox_escape_requires_perfect_scores():
    raw = json.loads((FIXTURES / "sandbox-escape" / "expected.json").read_text())
    assert raw["min_precision"] == 1.0
    assert raw["min_recall"] == 1.0
    assert raw["min_groundedness"] == 1.0
    assert raw["max_fp"] == 0


def test_wide_refactor_preserves_its_measured_baseline():
    raw = json.loads((FIXTURES / "wide-refactor" / "expected.json").read_text())
    assert raw["min_precision"] == 0.5
    assert raw["min_recall"] == 1.0
    assert raw["min_groundedness"] == 1.0
    assert raw["max_fp"] == 2
