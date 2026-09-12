"""The change map: what the diff touches, computed before the first turn.

The live trace that motivated this spent ten iterations guessing - four
speculative regexes that returned nothing, and not one read of a changed file.
Everything it was groping for is derivable from the checkout in Python. These
tests pin what is derived, that it is derived the same way every time, and the
two boundaries it must not cross: it may not make an out-of-scope file
readable, and it may not become evidence.
"""

import pathlib
import textwrap

import pytest

from agent.changemap import ChangeMap, build
from core.diff import changed_paths, hunk_ranges

CORE = '''\
"""Engine."""


class Engine:
    def __init__(self, name):
        self._name = name

    def run(self, payload):
        """Run one payload."""
        return self._name + payload

    def stop(self):
        return None
'''

USER = '''\
from pkg.core import Engine


def go():
    engine = Engine("x")
    return engine.run("y")
'''

# Uses `.run` and defines `run`, but never imports pkg.core. A bare name match
# would report both; neither has anything to do with Engine.run.
UNRELATED = '''\
import subprocess


def run(cmd):
    return subprocess.run(cmd)


def elsewhere(task):
    return task.run()
'''

TEST_FILE = '''\
from pkg.core import Engine


def test_run():
    assert Engine("a").run("b") == "ab"
'''

# Hunk covers `def run` (lines 8-10 of CORE).
DIFF = """diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -8,3 +8,3 @@
 context
"""


@pytest.fixture
def repo(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(CORE)
    (pkg / "user.py").write_text(USER)
    (pkg / "unrelated.py").write_text(UNRELATED)
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text(TEST_FILE)
    return tmp_path


@pytest.fixture
def built(repo):
    return build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF))


# -- changed symbols -------------------------------------------------------


def test_the_changed_symbol_is_identified(built):
    assert [s.name for s in built.symbols] == ["Engine.run"]


def test_the_symbol_carries_its_real_line_numbers(built):
    symbol = built.symbols[0]
    assert (symbol.line, symbol.end_line) == (8, 10)
    assert CORE.splitlines()[symbol.line - 1].strip() == "def run(self, payload):"


def test_a_changed_method_is_reported_not_its_whole_class(built):
    """Innermost wins: a method changing is not the class changing."""
    assert [s.kind for s in built.symbols] == ["method"]
    assert "Engine" not in {s.name for s in built.symbols}


def test_untouched_symbols_are_not_reported(built):
    names = {s.name for s in built.symbols}
    assert "Engine.stop" not in names and "Engine.__init__" not in names


def test_a_module_level_function_is_reported_as_a_function(repo):
    diff = DIFF.replace("@@ -8,3 +8,3 @@", "@@ -4,2 +4,2 @@").replace(
        "pkg/core.py", "pkg/unrelated.py"
    )
    out = build(str(repo), changed_paths(diff), hunk_ranges(diff))
    assert [(s.name, s.kind) for s in out.symbols] == [("run", "function")]


# -- references / call sites -----------------------------------------------


def test_a_call_site_is_found(built):
    refs = built.symbols[0].references
    assert ("pkg/user.py", 6, "use") in [(r.path, r.line, r.kind) for r in refs]


def test_the_reference_carries_the_source_line(built):
    ref = next(r for r in built.symbols[0].references if r.path == "pkg/user.py")
    assert ref.text == "return engine.run(\"y\")"


def test_a_same_named_symbol_in_an_unrelated_file_is_not_reported(built):
    """`Engine.run` must not "match" `subprocess.run` or a bare `task.run()`.

    A map that points at those sends the agent chasing unrelated code, which
    is the failure it exists to prevent.
    """
    assert all(r.path != "pkg/unrelated.py" for r in built.symbols[0].references)


def test_references_inside_the_diff_are_not_repeated(repo):
    """The agent can already read those; the map is for the blind spots."""
    out = build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF))
    assert all(r.path != "pkg/core.py" for r in out.symbols[0].references)


def test_a_symbol_with_no_outside_references_says_so(repo):
    diff = DIFF.replace("@@ -8,3 +8,3 @@", "@@ -12,2 +12,2 @@")   # Engine.stop
    out = build(str(repo), changed_paths(diff), hunk_ranges(diff))
    assert out.symbols[0].name == "Engine.stop"
    assert out.symbols[0].references == ()
    assert "(no references outside this diff)" in out.render()


# -- importers -------------------------------------------------------------


def test_importers_are_found(built):
    assert {r.path for r in built.importers} == {"pkg/user.py", "tests/test_core.py"}


def test_an_importer_carries_its_line_and_text(built):
    ref = next(r for r in built.importers if r.path == "pkg/user.py")
    assert (ref.line, ref.kind) == (1, "import")
    assert ref.text == "from pkg.core import Engine"


def test_a_relative_import_is_resolved(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(CORE)
    (pkg / "sibling.py").write_text("from .core import Engine\n")
    out = build(str(tmp_path), changed_paths(DIFF), hunk_ranges(DIFF))
    assert [r.path for r in out.importers] == ["pkg/sibling.py"]


def test_a_similarly_named_module_is_not_an_importer(tmp_path):
    """`pkg.corex` is not `pkg.core`."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(CORE)
    (pkg / "corex.py").write_text("X = 1\n")
    (pkg / "other.py").write_text("from pkg.corex import X\n")
    out = build(str(tmp_path), changed_paths(DIFF), hunk_ranges(DIFF))
    assert out.importers == ()


# -- tests -----------------------------------------------------------------


def test_a_relevant_test_is_identified(built):
    assert [r.path for r in built.tests] == ["tests/test_core.py"]


def test_tests_are_rendered_under_their_own_heading(built):
    assert "Tests outside this diff that name something this diff changed:" in built.render()


def test_a_repo_with_no_tests_reports_none(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(CORE)
    out = build(str(tmp_path), changed_paths(DIFF), hunk_ranges(DIFF))
    assert out.tests == ()
    assert "Tests outside" not in out.render()


# -- multiple changed files ------------------------------------------------


MULTI_DIFF = """diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -8,3 +8,3 @@
 context
diff --git a/pkg/user.py b/pkg/user.py
--- a/pkg/user.py
+++ b/pkg/user.py
@@ -4,3 +4,3 @@
 context
"""


def test_symbols_from_several_changed_files_are_all_reported(repo):
    out = build(str(repo), changed_paths(MULTI_DIFF), hunk_ranges(MULTI_DIFF))
    assert [(s.path, s.name) for s in out.symbols] == [
        ("pkg/core.py", "Engine.run"),
        ("pkg/user.py", "go"),
    ]


def test_a_file_that_became_in_scope_stops_being_an_outside_reference(repo):
    """pkg/user.py is changed here, so its call site is readable and is not
    listed as an outside reference."""
    out = build(str(repo), changed_paths(MULTI_DIFF), hunk_ranges(MULTI_DIFF))
    engine_run = next(s for s in out.symbols if s.name == "Engine.run")
    assert all(r.path != "pkg/user.py" for r in engine_run.references)


# -- empty / degraded ------------------------------------------------------


def test_no_workspace_yields_an_empty_map():
    assert build(None, changed_paths(DIFF), hunk_ranges(DIFF)) == ChangeMap()


def test_a_missing_checkout_yields_an_empty_map():
    assert build("/nonexistent/path/xyz", changed_paths(DIFF), hunk_ranges(DIFF)).empty


def test_an_empty_map_renders_as_nothing():
    assert ChangeMap().render() == ""


def test_an_unparseable_changed_file_is_named_not_fatal(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("def broken(:\n")
    out = build(str(tmp_path), changed_paths(DIFF), hunk_ranges(DIFF))
    assert out.unparsed == ("pkg/core.py",)
    assert "could not be parsed" in out.render()


def test_a_deleted_file_is_skipped(tmp_path):
    diff = (
        "diff --git a/pkg/core.py b/pkg/core.py\n"
        "--- a/pkg/core.py\n+++ /dev/null\n@@ -1,3 +0,0 @@\n-gone\n"
    )
    assert build(str(tmp_path), changed_paths(diff), hunk_ranges(diff)).empty


def test_vendored_directories_are_never_walked(repo):
    vendor = repo / ".venv" / "lib"
    vendor.mkdir(parents=True)
    (vendor / "shim.py").write_text("from pkg.core import Engine\n")
    out = build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF))
    assert all(".venv" not in r.path for r in out.importers)


# -- determinism -----------------------------------------------------------


def test_the_map_is_identical_across_repeated_builds(repo):
    runs = [
        build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF)).render()
        for _ in range(5)
    ]
    assert len(set(runs)) == 1


def test_everything_is_sorted(built):
    paths = [r.path for r in built.importers]
    assert paths == sorted(paths)
    for symbol in built.symbols:
        keys = [(r.path, r.line) for r in symbol.references]
        assert keys == sorted(keys)


def test_the_map_is_bounded(repo):
    from agent import changemap

    assert len(built_render(repo)) <= changemap.MAX_RENDER_CHARS + 40


def built_render(repo):
    return build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF)).render()


# -- the two boundaries ----------------------------------------------------


def test_the_map_does_not_make_an_outside_file_readable(repo):
    """It reports pkg/user.py; the scope jail must still refuse it."""
    from agent.tools import read_file

    out = build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF))
    named = {r.path for r in out.importers}
    assert "pkg/user.py" in named

    refusal = read_file(
        repo_root=str(repo), scope=changed_paths(DIFF), path="pkg/user.py"
    )
    assert refusal.startswith("error: pkg/user.py is not part of this diff")
    assert "Engine" not in refusal


def test_map_text_cannot_ground_a_finding(repo):
    """The map is in the message, never in the corpus, so quoting it proves
    nothing - exactly like the elision markers in Phase 3."""
    from agent.grounding import ground
    from core.models import Finding

    rendered = build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF)).render()
    quoted = next(
        line.strip() for line in rendered.splitlines() if "pkg/user.py" in line
    )
    finding = Finding(
        severity="nit",
        category="maintainability",
        file="pkg/core.py",
        title="t",
        description="d",
        recommendation="r",
        evidence=quoted,
    )

    # The corpus a real run carries: the diff, plus tool results. Not the map.
    rows = ground([finding], DIFF, [])
    assert rows[0][1] == "ungrounded"


# -- it reaches the model --------------------------------------------------


def test_the_map_is_in_the_first_user_message(repo):
    from agent.nodes import assemble_context

    out = assemble_context({"diff": DIFF, "workspace": str(repo)})
    text = out["messages"][0]["content"][0]["text"]

    assert "CHANGE MAP" in text
    assert "pkg/user.py" in text


def test_the_map_sits_inside_the_cached_prefix(repo):
    """Phase 2 caches everything before the breakpoint on this block. The map
    is stable for the whole run, so it must be paid for once, not per turn."""
    from agent.nodes import assemble_context

    block = assemble_context({"diff": DIFF, "workspace": str(repo)})["messages"][0][
        "content"
    ][0]

    assert block["cache_control"] == {"type": "ephemeral"}
    assert "CHANGE MAP" in block["text"]


def test_the_map_precedes_the_diff(repo):
    from agent.nodes import assemble_context

    text = assemble_context({"diff": DIFF, "workspace": str(repo)})["messages"][0][
        "content"
    ][0]["text"]

    assert text.index("CHANGED FILES") < text.index("CHANGE MAP") < text.index("DIFF:")


def test_assemble_context_still_works_without_a_workspace():
    """Nothing may break when the map cannot be computed."""
    from agent.nodes import assemble_context

    out = assemble_context({"diff": DIFF})
    text = out["messages"][0]["content"][0]["text"]

    assert "CHANGE MAP" not in text
    assert "DIFF:" in text
    assert out["scope"] == {"pkg/core.py": "modified"}


def test_assemble_context_does_not_touch_the_corpus(repo):
    """If the map ever entered the corpus it would become quotable evidence."""
    from agent.nodes import assemble_context

    out = assemble_context({"diff": DIFF, "workspace": str(repo)})
    assert "corpus" not in out


def test_the_map_warns_that_its_paths_are_not_readable(repo):
    rendered = build(str(repo), changed_paths(DIFF), hunk_ranges(DIFF)).render()
    assert "read_file still refuses it" in rendered
    assert "nothing quoted from this map counts as evidence" in rendered


# -- against the real fixtures ---------------------------------------------


def test_the_wide_refactor_map_surfaces_the_unreviewed_implementation():
    """store/memory.py implements the changed Store.put_many and imports
    store.base, and is not in the diff - so nothing in the run could see it."""
    from evals.runner import FIXTURES

    fixture = FIXTURES / "wide-refactor"
    diff = (fixture / "head.diff").read_text()
    out = build(str(fixture / "repo"), changed_paths(diff), hunk_ranges(diff))

    put_many = next(s for s in out.symbols if s.name == "Store.put_many")
    assert [(r.path, r.line, r.kind) for r in put_many.references] == [
        ("store/memory.py", 12, "def")
    ]
    assert [r.path for r in out.importers] == ["store/memory.py"]


def test_the_wide_refactor_map_is_small(tmp_path=None):
    from evals.runner import FIXTURES

    fixture = FIXTURES / "wide-refactor"
    diff = (fixture / "head.diff").read_text()
    rendered = build(
        str(fixture / "repo"), changed_paths(diff), hunk_ranges(diff)
    ).render()

    assert len(rendered) < 2500        # ~400 tokens, paid once inside the cache
