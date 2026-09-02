"""The hunk parser decides whether a comment posts or 422s. Untested until now."""

from gh.anchor import build_review, locate_in_diff, parse_anchorable

DIFF = """diff --git a/agent/tools.py b/agent/tools.py
index 1111111..2222222 100644
--- a/agent/tools.py
+++ b/agent/tools.py
@@ -15,8 +15,9 @@ def list_files(*, repo_root: str, path: str = ".") -> str:
 
 
 def read_file(*, repo_root: str, path: str) -> str:
-    return (Path(repo_root) / path).read_text()
+    return Path(path).read_text()
+    # no jail
 
 
 def search_code(*, repo_root: str, pattern: str, path: str = ".") -> str:
"""


def test_context_and_added_lines_are_anchorable():
    a = parse_anchorable(DIFF)["agent/tools.py"]
    # hunk starts at new-file line 15; two blank context lines, then the def
    assert 15 in a and 16 in a and 17 in a
    assert 18 in a  # the added `return Path(path)...`
    assert 19 in a  # the added comment


def test_removed_lines_do_not_consume_a_right_line():
    a = parse_anchorable(DIFF)["agent/tools.py"]
    # Body has 8 new-side lines (3 context, 2 added, 3 context) => 15..22.
    # The `-` line consumes no right-side number.
    # Note the hunk header claims 9; the parser counts real content instead,
    # which is correct - malformed headers are common in hand-written diffs.
    assert max(a) == 22
    assert 23 not in a


def test_locate_returns_correct_new_file_line():
    loc = locate_in_diff(DIFF, "    return Path(path).read_text()")
    assert loc is not None
    path, line = loc
    assert path == "agent/tools.py"
    assert line == 18


def _finding(evidence, verdict):
    return {
        "severity": "blocker",
        "category": "security",
        "file": "agent/tools.py",
        "title": "no path jail",
        "description": "d",
        "recommendation": "r",
        "evidence": evidence,
        "verdict": verdict,
    }


def test_grounded_finding_becomes_an_inline_comment():
    published, payload, tally = build_review(
        [_finding("    return Path(path).read_text()", "grounded")],
        DIFF,
        "abc123",
        "Two issues found.",
    )
    assert tally == {"inline": 1, "summary": 0, "dropped": 0}
    assert payload["comments"][0]["path"] == "agent/tools.py"
    assert payload["comments"][0]["line"] == 18
    assert payload["comments"][0]["side"] == "RIGHT"
    assert payload["commit_id"] == "abc123"


def test_summary_suppressed_when_every_finding_is_dropped():
    published, payload, tally = build_review(
        [_finding("a prose description of the bug", "ungrounded")],
        DIFF,
        "abc123",
        "CRITICAL: this PR has severe security issues.",
    )
    assert tally["dropped"] == 1
    assert payload["comments"] == []
    # the unverified summary must NOT be published
    assert "CRITICAL" not in payload["body"]
    assert "discarded" in payload["body"]


def test_clean_review_still_posts():
    _, payload, tally = build_review([], DIFF, "abc123", "")
    assert payload["body"] == "Reviewed, nothing found."
    assert payload["comments"] == []
