"""The scope jail: tools refuse what is not in the diff.

The prompt already said "stay in scope". A live 2-file PR still spent all ten
iterations searching the repository and never read a changed file, because the
tools allowed it. These tests pin the tools as the authority.
"""

import pathlib
import tempfile

import pytest

from agent.nodes import _with_budget
from agent.tools import is_unproductive, read_file, search_code

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
    assert _with_budget(result, 4) == result


def test_budget_note_is_appended_to_a_refusal():
    out = _with_budget("error: not part of this diff", 3)
    assert out.endswith("[3 iterations left in this review]")


def test_budget_note_is_singular_on_the_last_iteration():
    assert _with_budget("", 1).endswith("[1 iteration left in this review]")


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
    assert "[7 iterations left in this review]" in body
