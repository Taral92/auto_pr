from evals.scoring import Expected, breakeven_precision, net_minutes, score

EXP = [
    Expected("jail", "agent/tools.py", "security", ["read_bytes()"], "blocker"),
    Expected("shell", "agent/tools.py", "security", ["os.system"], "blocker"),
]


def f(path, cat, evidence, anchored="inline", verdict="grounded"):
    return {
        "path": path, "category": cat, "evidence": evidence, "title": "t",
        "body": "b", "anchored": anchored, "verdict": verdict,
    }


def test_perfect_run():
    s = score(
        [f("agent/tools.py", "security", "raw = (Path(repo_root) / path).read_bytes()"),
         f("agent/tools.py", "security", "os.system(pattern[1:])")],
        EXP,
    )
    assert (s.tp, s.fp, s.fn) == (2, 0, 0)
    assert s.precision == 1.0 and s.recall == 1.0


def test_dropped_findings_are_not_false_positives():
    """The gate protects the developer. It must not be punished for working."""
    s = score([f("x.py", "correctness", "nonsense", anchored="dropped",
                 verdict="ungrounded")], [])
    assert s.fp == 0
    assert s.ungrounded == 1


def test_misses_are_recall_not_precision():
    s = score([f("agent/tools.py", "security", "os.system(pattern[1:])")], EXP)
    assert (s.tp, s.fp, s.fn) == (1, 0, 1)
    assert s.precision == 1.0 and s.recall == 0.5


def test_noise_is_a_false_positive():
    s = score([f("README.md", "maintainability", "placeholder text here")], [])
    assert (s.tp, s.fp) == (0, 1)


def test_one_finding_cannot_claim_two_expectations():
    dup = f("agent/tools.py", "security", "os.system(pattern[1:])")
    s = score([dup], EXP)
    assert s.tp == 1 and s.fn == 1


# --- the number the project exists to produce ---------------------------

def test_good_run_saves_time():
    s = score(
        [f("agent/tools.py", "security", "raw = (Path(repo_root) / path).read_bytes()"),
         f("agent/tools.py", "security", "os.system(pattern[1:])")],
        EXP,
    )
    t = net_minutes(s, EXP)
    assert t["net_min"] == 24.0          # 2 blockers x 12 min, no tax
    assert t["verdict"] == "saves time"


def test_noisy_run_costs_time():
    """2 real findings buried in 20 wrong ones is a net loss."""
    findings = [
        f("agent/tools.py", "security", "raw = (Path(repo_root) / path).read_bytes()"),
        f("agent/tools.py", "security", "os.system(pattern[1:])"),
    ] + [f(f"noise{i}.py", "maintainability", "x" * 30) for i in range(20)]
    t = net_minutes(score(findings, EXP), EXP)
    assert t["net_min"] == 24.0 - 30.0   # 20 FP x 1.5 min
    assert t["verdict"] == "costs time"


def test_silent_agent_is_neutral_not_negative():
    """Missing a bug is 0, not negative - unaided, the dev misses it too."""
    t = net_minutes(score([], EXP), EXP)
    assert t["net_min"] == 0.0
    assert t["verdict"] == "neutral"      # silence is not a cost


def test_breakeven_precision_is_reported():
    # 12 min to find, 1.5 min to dismiss -> break even at 1.5/13.5 = 11%
    assert breakeven_precision(EXP) == 0.111
