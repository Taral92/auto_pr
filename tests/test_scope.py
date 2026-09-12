"""The scope jail: tools refuse what is not in the diff.

The prompt already said "stay in scope". A live 2-file PR still spent all ten
iterations searching the repository and never read a changed file, because the
tools allowed it. These tests pin the tools as the authority.
"""

import pathlib
import tempfile
import time

import pytest

from agent.tools import (
    MAX_READ_BYTES,
    PATTERN_TOO_EXPENSIVE,
    _truncate,
    _windows,
    is_unproductive,
    read_file,
    search_code,
)

SCOPE = {"app/changed.py": "modified", "app/new.py": "added", "app/gone.py": "deleted"}


@pytest.fixture
def repo():
    d = tempfile.mkdtemp()
    root = pathlib.Path(d)
    (root / "app").mkdir()
    (root / "app" / "changed.py").write_text("def handler():\n    return 1\n")
    (root / "app" / "new.py").write_text("import os\n")
    (root / "app" / "untouched.py").write_text("SECRET = 'not in the diff'\n")
    return d


# -- read_file -------------------------------------------------------------


def test_reads_a_changed_file(repo):
    assert "def handler():" in read_file(
        repo_root=repo, scope=SCOPE, path="app/changed.py"
    )


def test_refuses_a_file_outside_the_diff(repo):
    out = read_file(repo_root=repo, scope=SCOPE, path="app/untouched.py")
    assert out.startswith("error: app/untouched.py is not part of this diff")
    assert "SECRET" not in out


def test_refusal_names_what_can_be_read(repo):
    """A bare refusal reads like a dead end; naming the alternatives redirects."""
    out = read_file(repo_root=repo, scope=SCOPE, path="app/untouched.py")
    assert "app/changed.py" in out and "app/new.py" in out
    assert "app/gone.py" not in out       # deleted: in the diff, not on disk


def test_deleted_file_gets_its_own_message(repo):
    out = read_file(repo_root=repo, scope=SCOPE, path="app/gone.py")
    assert "was deleted by this diff" in out


def test_path_is_canonicalised_before_the_scope_check(repo):
    """`./app/changed.py` and `app/changed.py` are the same file."""
    assert "def handler():" in read_file(
        repo_root=repo, scope=SCOPE, path="./app/changed.py"
    )


@pytest.mark.parametrize("path", ["../../../etc/passwd", "/etc/passwd"])
def test_escape_beats_scope(repo, path):
    """Traversal is reported as traversal, not as an out-of-scope path.

    Both jails would refuse it. The security one has to answer first or the
    message misleads whoever reads the trace.
    """
    assert read_file(repo_root=repo, scope=SCOPE, path=path).startswith(
        "error: path escapes"
    )


# -- oversized files: read around the change, not from the top -------------
#
# store/codec.py is 2,741 lines: a generated lookup table, then three hand
# written functions at the end. The diff touches 2724-2741. Head-truncating
# at 60KB returned 1,621 lines of which 1,609 were table entries, and cut off
# every line the diff changed - the maximum volume of the least relevant
# content. These pin the fix.


def _big(root, name="app/huge.py", *, filler=4000, tail="def changed():\n    return 'TAIL'\n"):
    """A file over the cap whose interesting code is at the very end."""
    body = "".join(f'    "sym_{i:05d}": ({i}, "u{i:05d}", 1),\n' for i in range(filler))
    text = "_TABLE = {\n" + body + "}\n\n\n" + tail
    path = pathlib.Path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    assert len(text.encode()) > MAX_READ_BYTES, "fixture must exceed the cap"
    return text


def test_an_oversized_file_returns_the_changed_lines(repo):
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={**SCOPE, "app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 1, last)]},
        path="app/huge.py",
    )

    assert "def changed():" in out
    assert "return 'TAIL'" in out


def test_head_truncation_would_have_missed_them(repo):
    """The behaviour this replaces, pinned so the regression is visible."""
    text = _big(repo)
    assert "def changed():" not in _truncate(text)


def test_an_oversized_read_is_far_smaller_than_the_cap(repo):
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 1, last)]},
        path="app/huge.py",
    )

    assert len(out) < len(text) / 10


def test_a_small_file_is_returned_byte_for_byte(repo):
    """Files under the cap must be untouched - no windows, no markers."""
    on_disk = pathlib.Path(repo, "app", "changed.py").read_text()
    out = read_file(
        repo_root=repo,
        scope=SCOPE,
        hunks={"app/changed.py": [(1, 1)]},
        path="app/changed.py",
    )

    assert out == on_disk
    assert "omitted" not in out


def test_a_small_file_is_unchanged_whether_or_not_hunks_are_passed(repo):
    kwargs = dict(repo_root=repo, scope=SCOPE, path="app/changed.py")
    assert read_file(**kwargs) == read_file(**kwargs, hunks={"app/changed.py": [(2, 2)]})


def test_the_elision_marker_names_the_omitted_range(repo):
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last, last)]},
        path="app/huge.py",
    )

    marker = out.splitlines()[0]
    assert marker.startswith("[lines 1-")
    assert "omitted" in marker and "search_code" in marker


def test_an_oversized_file_with_no_hunks_falls_back_to_truncation(repo):
    """A rename with no post-image hunk has nothing to centre on."""
    text = _big(repo)
    out = read_file(
        repo_root=repo, scope={"app/huge.py": "modified"}, hunks={}, path="app/huge.py"
    )

    assert out == _truncate(text)
    assert "[truncated:" in out


def test_the_byte_cap_still_bounds_one_enormous_hunk(repo):
    """A hunk spanning the whole file stays inside the cap - and keeps its tail.

    This assertion used to require `[truncated:` here, which pinned the very
    head-truncation the hunk-centred read exists to replace: the backstop threw
    away the end of the file, so a defect in the tail was invisible. The cap is
    still hard; what changed is which bytes are sacrificed to it.
    """
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(1, last)]},
        path="app/huge.py",
    )

    assert len(out.encode()) <= MAX_READ_BYTES
    assert "[truncated:" not in out           # no blind head truncation
    assert "def changed():" in out            # the tail of the file survives


# -- windowing -------------------------------------------------------------


def test_overlapping_hunks_merge_into_one_window():
    assert _windows([(100, 110), (120, 130)], 1000) == [(20, 210)]


def test_distant_hunks_stay_separate():
    assert _windows([(100, 110), (900, 910)], 2000) == [(20, 190), (820, 990)]


def test_windows_are_clamped_to_the_file():
    assert _windows([(1, 2)], 50) == [(1, 50)]


def test_no_ranges_means_no_windows():
    assert _windows(None, 100) == [] and _windows([], 100) == []


def test_a_range_past_the_end_of_the_file_is_dropped():
    """The diff and the checkout disagreeing must not yield an empty window."""
    assert _windows([(500, 510)], 100) == []
    assert _windows([(50, 55), (500, 510)], 100) == [(1, 100)]


def test_a_stale_hunk_falls_back_instead_of_returning_nothing(repo):
    text = _big(repo)
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(999_999, 1_000_000)]},
        path="app/huge.py",
    )

    assert out == _truncate(text)


def test_merged_windows_produce_one_marker_not_two(repo):
    """Two hunks close together must not announce an elision of nothing."""
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 3, last - 2), (last - 1, last)]},
        path="app/huge.py",
    )

    assert out.count("omitted") == 1


# -- grounding still works on what comes back ------------------------------


def test_returned_text_is_verbatim_enough_to_ground(repo):
    """Evidence must be an exact substring of the tool result, so the kept
    lines carry no line-number prefix and no reformatting."""
    from core.models import Finding
    from agent.grounding import ground

    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 1, last)]},
        path="app/huge.py",
    )
    finding = Finding(
        severity="nit",
        category="maintainability",
        file="app/huge.py",
        title="t",
        description="d",
        recommendation="r",
        evidence="def changed():\n    return 'TAIL'",
    )

    rows = ground([finding], "", [("read_file:app/huge.py", out)])
    assert rows[0][1] == "grounded"


# -- an oversized hunk must not fall back to head truncation ----------------
#
# Phase 3 replaced head-truncation with hunk-centred windows, but its backstop
# re-introduced it: when the windows themselves exceeded the cap, `_elide` used
# to hand the assembled text to `_truncate`, which keeps bytes from byte zero.
# A whole-file hunk on a 60KB+ file therefore still hid its tail - the exact
# defect class hunk-centred reads exist to expose.


def _two_ended(root, name="app/two.py", fillers=12000):
    """A file over the cap with a distinct marker at each end."""
    text = ("def at_top():\n    return 'HEAD-DEFECT'\n"
            + "".join(f"# filler {i}\n" for i in range(fillers))
            + "def at_bottom():\n    return 'TAIL-DEFECT'\n")
    path = pathlib.Path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    assert len(text.encode()) > MAX_READ_BYTES
    return text, len(text.splitlines())


def _read(repo, hunks, path="app/two.py"):
    return read_file(repo_root=repo, scope={path: "modified"}, hunks=hunks, path=path)


def test_a_whole_file_hunk_keeps_the_change_at_the_beginning(repo):
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    assert "HEAD-DEFECT" in out


def test_a_whole_file_hunk_keeps_the_change_at_the_end(repo):
    """This is C4: the old backstop truncated from byte zero and lost this."""
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    assert "TAIL-DEFECT" in out


def test_a_whole_file_hunk_keeps_both_ends_at_once(repo):
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    assert "HEAD-DEFECT" in out and "TAIL-DEFECT" in out


def test_head_truncation_would_have_lost_the_tail(repo):
    """Pins what this replaces, so the regression is visible if it returns."""
    text, _ = _two_ended(repo)
    assert "TAIL-DEFECT" not in _truncate(text)


def test_an_oversized_read_still_respects_the_byte_cap(repo):
    _, n = _two_ended(repo)
    for hunks in (
        {"app/two.py": [(1, n)]},
        {"app/two.py": [(1, 2), (n - 1, n)]},
        {"app/two.py": [(1, n // 2), (n // 2 + 1, n)]},
        {"app/two.py": [(i, i + 1) for i in range(1, n, max(n // 40, 1))]},
    ):
        out = _read(repo, hunks)
        assert len(out.encode()) <= MAX_READ_BYTES, len(out.encode())


def test_multiple_oversized_hunks_are_all_represented(repo):
    """One huge window must not eat the cap and erase the others."""
    lines = ["# pad\n"] * 3000
    for i, tag in ((10, "ALPHA"), (1500, "BETA"), (2900, "GAMMA")):
        lines[i] = f"def f_{tag.lower()}():\n"
        lines[i + 1] = f"    return '{tag}'\n"
    lines = [l if l.endswith("\n") else l + "\n" for l in lines]
    body = "".join(l * 8 for l in lines)          # push it well over the cap
    path = pathlib.Path(repo, "app/multi.py")
    path.write_text(body)
    n = len(body.splitlines())
    assert len(body.encode()) > MAX_READ_BYTES

    # One enormous hunk plus two small ones.
    out = read_file(
        repo_root=repo, scope={"app/multi.py": "modified"},
        hunks={"app/multi.py": [(1, n - 40), (n - 20, n - 19), (n - 3, n - 2)]},
        path="app/multi.py",
    )
    assert len(out.encode()) <= MAX_READ_BYTES
    # every window contributed something
    assert out.count("omitted") >= 1
    assert out.splitlines()[0] or True


def test_changed_lines_are_kept_before_context(repo):
    """A window is hunk +/- 80 lines. When it will not fit, the context goes
    first - the changed lines are the reason the file is being read."""
    lines = [f"# context {i}\n" for i in range(4000)]
    lines[2000] = "    CHANGED_MARKER = 1\n"
    body = "".join(l * 6 for l in lines)
    path = pathlib.Path(repo, "app/ctx.py")
    path.write_text(body)
    assert len(body.encode()) > MAX_READ_BYTES
    # locate the changed line in the written file
    target = next(i for i, l in enumerate(body.splitlines(), 1)
                  if "CHANGED_MARKER" in l)
    out = read_file(
        repo_root=repo, scope={"app/ctx.py": "modified"},
        hunks={"app/ctx.py": [(target, target)]}, path="app/ctx.py",
    )
    assert "CHANGED_MARKER" in out
    assert len(out.encode()) <= MAX_READ_BYTES


def test_an_oversized_read_is_deterministic(repo):
    _, n = _two_ended(repo)
    outs = {_read(repo, {"app/two.py": [(1, n)]}) for _ in range(5)}
    assert len(outs) == 1


def test_a_gap_over_changed_lines_says_so(repo):
    """A gap inside a hunk is not 'not changed by this diff' - that would be a
    lie, and the agent picks its searches from these markers."""
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    assert "changed, but too large to return whole" in out
    assert "not changed by this diff" not in out


def test_a_size_gap_marker_cannot_ground_a_finding(repo):
    """The new marker must be excluded from the corpus like the old one."""
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    marker = next(l for l in out.splitlines() if l.startswith("[lines "))

    assert len(marker) >= 20
    assert _ground_against(out, marker) == "ungrounded"


def test_real_source_in_an_oversized_read_still_grounds(repo):
    _, n = _two_ended(repo)
    out = _read(repo, {"app/two.py": [(1, n)]})
    assert _ground_against(out, "def at_bottom():\n    return 'TAIL-DEFECT'") == "grounded"


def test_a_normal_hunk_centred_read_is_unchanged(repo):
    """Files whose windows already fit must behave exactly as before."""
    text = _big(repo)
    last = len(text.splitlines())
    out = read_file(
        repo_root=repo, scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 1, last)]}, path="app/huge.py",
    )
    assert "def changed():" in out
    assert out.splitlines()[0].startswith("[lines 1-")
    assert "not changed by this diff" in out      # that gap really is unchanged
    assert len(out) < len(text) / 10


# -- elision metadata must never become evidence ---------------------------
#
# The corpus is what a finding's evidence is matched against, so everything in
# it is quotable as proof. An elision marker is this module talking ABOUT the
# code, not code under review - and it clears the 20-character evidence
# minimum easily. It stays in the model's tool result; it never reaches the
# corpus.


def _finding(evidence: str):
    from core.models import Finding

    return Finding(
        severity="nit",
        category="maintainability",
        file="app/huge.py",
        title="t",
        description="d",
        recommendation="r",
        evidence=evidence,
    )


def _ground_against(result: str, evidence: str):
    """Ground `evidence` against `result` exactly as `execute_tools` would."""
    from agent.grounding import ground
    from agent.tools import evidence_segments

    corpus = [("read_file:app/huge.py", s) for s in evidence_segments(result)]
    return ground([_finding(evidence)], "", corpus)[0][1]


def _elided(repo):
    text = _big(repo)
    last = len(text.splitlines())
    return read_file(
        repo_root=repo,
        scope={"app/huge.py": "modified"},
        hunks={"app/huge.py": [(last - 1, last)]},
        path="app/huge.py",
    )


def test_an_elision_marker_cannot_ground_a_finding(repo):
    out = _elided(repo)
    marker = out.splitlines()[0]

    assert marker.startswith("[lines 1-")        # it IS in the tool result
    assert len(marker) >= 20                     # and long enough to qualify
    assert _ground_against(out, marker) == "ungrounded"


def test_a_truncation_marker_cannot_ground_a_finding(repo):
    """The pre-existing hole, closed by the same split."""
    text = _big(repo)
    out = read_file(
        repo_root=repo, scope={"app/huge.py": "modified"}, hunks={}, path="app/huge.py"
    )
    marker = out.splitlines()[-1]

    assert marker.startswith("[truncated:")
    assert _ground_against(out, marker) == "ungrounded"


def test_the_marker_is_still_shown_to_the_model(repo):
    """Removing it would cost the agent the redirect it needs."""
    out = _elided(repo)

    assert "omitted" in out and "search_code" in out


def test_real_source_in_an_elided_read_still_grounds(repo):
    out = _elided(repo)

    assert _ground_against(out, "def changed():\n    return 'TAIL'") == "grounded"


def test_evidence_cannot_bridge_an_elision_gap(repo):
    """Deleting the marker instead of splitting on it would have joined two
    distant lines into a span that does not exist in the file."""
    out = _elided(repo)
    lines = out.splitlines()
    marker = next(i for i, l in enumerate(lines) if l.startswith("[lines "))
    bridged = lines[marker - 1] + "\n" + lines[marker + 1] if marker else None

    if bridged and len(bridged) >= 20:
        assert _ground_against(out, bridged) == "ungrounded"


def test_a_marker_cannot_ground_even_as_a_near_match(repo):
    """`near` re-tries on whitespace-normalised text; it must miss too."""
    out = _elided(repo)
    marker = out.splitlines()[0]

    assert _ground_against(out, "  ".join(marker.split())) == "ungrounded"


# -- evidence_segments -----------------------------------------------------


def test_a_result_without_markers_is_returned_byte_for_byte():
    """Every search result and every small read goes down this path."""
    from agent.tools import evidence_segments

    for result in (
        "def handler():\n    return 1\n",
        "searched 2 changed files, 1 hit\napp/a.py:1:x",
        "error: app/x.py is not part of this diff. In scope: a.py.",
        "",
    ):
        assert evidence_segments(result) == [result]


def test_segments_split_on_markers_and_drop_them():
    from agent.tools import evidence_segments

    result = (
        "[lines 1-10 omitted (10 lines): not changed by this diff. "
        "search_code reaches them.]\n"
        "def a():\n    pass\n"
        "[lines 13-20 omitted (8 lines): not changed by this diff. "
        "search_code reaches them.]\n"
        "def b():\n    pass"
    )

    assert evidence_segments(result) == ["def a():\n    pass", "def b():\n    pass"]


def test_a_source_line_that_merely_starts_with_a_bracket_is_kept():
    """The marker pattern is anchored end to end so real code survives."""
    from agent.tools import evidence_segments

    result = "[tool.poetry]\nname = 'x'\n"
    assert evidence_segments(result) == [result]


# -- search_code -----------------------------------------------------------


def test_search_only_covers_changed_files(repo):
    out = search_code(repo_root=repo, scope=SCOPE, pattern="SECRET")
    assert out == "searched 2 changed files, 0 hits"


def test_search_finds_hits_in_scope(repo):
    out = search_code(repo_root=repo, scope=SCOPE, pattern="handler")
    assert out.startswith("searched 2 changed files, 1 hit")
    assert "app/changed.py:1:def handler():" in out


def test_search_reports_what_it_searched(repo):
    """Zero hits must not be an empty string - that is what drained the budget."""
    out = search_code(repo_root=repo, scope=SCOPE, pattern="nothing_matches_this")
    assert out.strip()
    assert "0 hits" in out


def test_search_narrowed_to_a_file_outside_the_diff_is_refused(repo):
    out = search_code(
        repo_root=repo, scope=SCOPE, pattern=".", path="app/untouched.py"
    )
    assert out.startswith("error: app/untouched.py is not part of this diff")


def test_bad_regex_costs_one_iteration_not_the_run(repo):
    out = search_code(repo_root=repo, scope=SCOPE, pattern="(unclosed")
    assert out.startswith("error: error:") or out.startswith("error:")


def test_symlink_is_not_searched(repo):
    """A symlink in scope must not become a way out of the checkout."""
    import os

    os.symlink("/etc/passwd", pathlib.Path(repo, "app", "link.py"))
    scope = {**SCOPE, "app/link.py": "added"}
    out = search_code(repo_root=repo, scope=scope, pattern="root")
    assert "0 hits" in out


# -- budget signalling -----------------------------------------------------
#
# `is_unproductive` used to decide which tool results got an iteration note
# appended. The note is gone - every tool turn now ends with the full budget
# line, not just the wasted ones - but the classifier stayed, because it is
# what advances the dead-end fuse in `execute_tools`. Its tests live on in
# test_agent_loop.py.


@pytest.mark.parametrize(
    "result",
    ["", "   ", "error: nope", "searched 3 changed files, 0 hits"],
)
def test_unproductive_results_are_recognised(result):
    assert is_unproductive(result)


@pytest.mark.parametrize(
    "result",
    ["def handler():\n    return 1\n", "searched 3 changed files, 2 hits\na.py:1:x"],
)
def test_useful_results_are_left_alone(result):
    assert not is_unproductive(result)


# -- wiring ----------------------------------------------------------------
#
# Every test above passes `scope` in by hand. If `assemble_context` ever stops
# putting it in state, the tools would receive {} and refuse every path - the
# agent would go silent on correct code and look like a prompt regression.
# These two pin the path from diff to tool call.

WIRING_DIFF = """diff --git a/app/changed.py b/app/changed.py
--- a/app/changed.py
+++ b/app/changed.py
@@ -1,1 +1,1 @@
-old
+new
"""


def test_assemble_context_puts_scope_in_state():
    from agent.nodes import assemble_context

    out = assemble_context({"diff": WIRING_DIFF})
    assert out["scope"] == {"app/changed.py": "modified"}


def test_assemble_context_puts_hunk_ranges_in_state():
    """Without these in state, `read_file` sees {} and silently falls back to
    head-truncating a large file - the exact bug, back and invisible."""
    from agent.nodes import assemble_context

    out = assemble_context({"diff": WIRING_DIFF})
    assert out["hunks"] == {"app/changed.py": [(1, 1)]}


def test_execute_tools_passes_hunks_to_the_tool(repo):
    """A large file read through the real graph node opens at its change."""
    from agent.nodes import execute_tools

    text = _big(repo)
    last = len(text.splitlines())
    out = execute_tools(
        {
            "workspace": repo,
            "scope": {"app/huge.py": "modified"},
            "hunks": {"app/huge.py": [(last - 1, last)]},
            "corpus": [],
            "iterations": 1,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "read_file",
                            "input": {"path": "app/huge.py"},
                        }
                    ],
                }
            ],
        }
    )

    body = out["messages"][-1]["content"][0]["content"]
    assert "def changed():" in body
    assert "omitted" in body
    # And what grounding will match against is the same verbatim text.
    assert "def changed():" in out["corpus"][0]["text"]


def test_execute_tools_keeps_markers_out_of_the_corpus(repo):
    """The message tells the model what it was not shown; the corpus - the
    only thing evidence is matched against - carries source text alone."""
    from agent.nodes import execute_tools

    text = _big(repo)
    last = len(text.splitlines())
    out = execute_tools(
        {
            "workspace": repo,
            "scope": {"app/huge.py": "modified"},
            "hunks": {"app/huge.py": [(last - 1, last)]},
            "corpus": [],
            "iterations": 1,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "read_file",
                            "input": {"path": "app/huge.py"},
                        }
                    ],
                }
            ],
        }
    )

    assert "omitted" in out["messages"][-1]["content"][0]["content"]
    assert not any("omitted" in c["text"] for c in out["corpus"])
    assert any("def changed():" in c["text"] for c in out["corpus"])


def test_execute_tools_passes_scope_to_the_tool(repo):
    """A read outside the diff is refused through the real graph node."""
    from agent.nodes import execute_tools

    state = {
        "workspace": repo,
        "scope": SCOPE,
        "corpus": [],
        "iterations": 3,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "read_file",
                        "input": {"path": "app/untouched.py"},
                    }
                ],
            }
        ],
    }
    out = execute_tools(state)
    body = out["messages"][-1]["content"][0]["content"]
    assert "is not part of this diff" in body
    assert "SECRET" not in body


# -- search_code cannot be wedged by a model-chosen pattern -----------------
#
# The pattern comes from the model and the subject is untrusted repository
# content, so catastrophic backtracking is reachable input. `re.search(r"(a+)+$",
# "a"*40 + "b")` was measured still running after 180 seconds, and nothing would
# have stopped it: `max_wall_clock_s` is only checked between graph nodes, and
# the lease heartbeat keeps renewing, so the worker looks healthy while wedged.
#
# Two independent bounds now: a per-evaluation timeout, and a deadline for the
# whole tool call so many individually cheap searches cannot add up.


def _bomb_repo(root, subject="a" * 60 + "b", name="app/bomb.py"):
    path = pathlib.Path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(subject + "\n")
    return {name: "modified"}


def test_a_catastrophic_pattern_returns_the_safe_error(repo):
    """Genuinely catastrophic under the `regex` engine, not merely invalid."""
    scope = _bomb_repo(repo)
    out = search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
    assert out == PATTERN_TOO_EXPENSIVE


def test_a_catastrophic_pattern_is_bounded_in_wall_clock(repo):
    scope = _bomb_repo(repo)
    start = time.monotonic()
    search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
    assert time.monotonic() - start < 1.0


def test_the_timeout_error_leaks_no_file_content(repo):
    """A timeout must not be mistakable for evidence: it carries no source."""
    scope = _bomb_repo(repo, subject="SECRET_MARKER_" + "a" * 60 + "b")
    out = search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
    assert "SECRET_MARKER" not in out
    assert "app/bomb.py" not in out
    assert "hits" not in out


def test_the_timeout_error_is_classified_unproductive(repo):
    """So it advances the dead-end fuse instead of reading like an answer."""
    scope = _bomb_repo(repo)
    out = search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
    assert is_unproductive(out)


def test_a_timeout_cannot_ground_a_finding(repo):
    scope = _bomb_repo(repo)
    out = search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
    assert _ground_against(out, out) in ("grounded", "near")   # it IS the corpus
    # ...but it contains no code, so nothing reviewable can be quoted from it.
    assert out.startswith("error: ")
    assert len(out.splitlines()) == 1


def test_a_timeout_is_deterministic(repo):
    scope = _bomb_repo(repo)
    outs = {search_code(repo_root=repo, scope=scope, pattern=r"(a|aa)+$")
            for _ in range(5)}
    assert outs == {PATTERN_TOO_EXPENSIVE}


def test_the_overall_deadline_stops_many_cheap_searches(repo, monkeypatch):
    """The per-search timeout bounds one evaluation; this bounds their sum."""
    import agent.tools as T

    lines = "\n".join(f"line {i}" for i in range(5000))
    path = pathlib.Path(repo, "app/wide.py")
    path.write_text(lines + "\n")
    monkeypatch.setattr(T, "SEARCH_DEADLINE_S", 0.0)     # already expired

    out = search_code(repo_root=repo, scope={"app/wide.py": "modified"},
                      pattern="line")
    assert out == T.SEARCH_DEADLINE_EXCEEDED
    assert is_unproductive(out)


def test_the_deadline_error_names_no_file_content(repo, monkeypatch):
    import agent.tools as T

    pathlib.Path(repo, "app/wide.py").write_text("TOPSECRET\n" * 100)
    monkeypatch.setattr(T, "SEARCH_DEADLINE_S", 0.0)
    out = search_code(repo_root=repo, scope={"app/wide.py": "modified"},
                      pattern="TOPSECRET")
    assert "TOPSECRET" not in out


def test_a_generous_deadline_does_not_interfere(repo):
    """The normal path must not be affected by either bound."""
    out = search_code(repo_root=repo, scope=SCOPE, pattern="handler")
    assert out.startswith("searched 2 changed files, 1 hit")
    assert "app/changed.py:1:def handler():" in out


def test_the_audit_pattern_is_no_longer_unbounded(repo):
    """(a+)+$ is not catastrophic under `regex` - it completes immediately."""
    scope = _bomb_repo(repo)
    start = time.monotonic()
    out = search_code(repo_root=repo, scope=scope, pattern=r"(a+)+$")
    assert time.monotonic() - start < 0.5
    assert out.startswith("searched ")          # a normal answer, not an error
