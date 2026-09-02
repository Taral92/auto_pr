"""Positive control: prove `grounded` is reachable.

Every run so far returned 0 grounded. That is indistinguishable from a gate
that always says ungrounded until this test exists.
"""

from agent.grounding import counts, ground
from core.models import Finding

DIFF = """diff --git a/agent/tools.py b/agent/tools.py
--- a/agent/tools.py
+++ b/agent/tools.py
@@ -15,8 +15,12 @@
 def read_file(*, repo_root: str, path: str) -> str:
-    return (Path(repo_root) / path).read_text()
+    return Path(path).read_text()
"""


def _f(evidence: str) -> Finding:
    return Finding(
        severity="blocker",
        category="security",
        file="agent/tools.py",
        title="t",
        description="d",
        recommendation="r",
        evidence=evidence,
    )


def test_verbatim_span_is_grounded():
    rows = ground([_f("+    return Path(path).read_text()")], DIFF, [])
    assert rows[0][1] == "grounded"
    assert rows[0][2] == "diff"


def test_grounded_from_a_tool_result():
    rows = ground(
        [_f("def read_file(*, repo_root: str, path: str) -> str:")],
        "",
        [("read_file:agent/tools.py", "x\ndef read_file(*, repo_root: str, path: str) -> str:\n")],
    )
    assert rows[0][1] == "grounded"
    assert rows[0][2] == "read_file:agent/tools.py"


def test_reflowed_whitespace_is_near_not_grounded():
    rows = ground([_f("+ return   Path(path).read_text()")], DIFF, [])
    assert rows[0][1] == "near"


def test_prose_description_is_ungrounded():
    # This is the shape every real run has produced so far.
    rows = ground([_f("Line 18 removes the repo_root prefix from read_file")], DIFF, [])
    assert rows[0][1] == "ungrounded"


def test_short_span_rejected_even_if_present():
    rows = ground([_f("Path")], DIFF, [])
    assert rows[0][1] == "ungrounded"


def test_counts_tally():
    rows = ground(
        [_f("+    return Path(path).read_text()"), _f("a description of the bug here")],
        DIFF,
        [],
    )
    assert counts(rows) == {"grounded": 1, "near": 0, "ungrounded": 1}
