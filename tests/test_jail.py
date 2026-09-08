"""The path jail: nothing outside the checkout, whatever the diff says.

This is the security boundary. Model-chosen tool calls run over untrusted
third-party code, so it is checked before the scope jail and independently of
it - a permissive scope must never widen it.
"""

import os
import pathlib
import tempfile

import pytest

from agent.tools import read_file, search_code

# Deliberately permissive: these tests must fail on the path jail alone.
OPEN_SCOPE = {"ok.txt": "modified", "sub/deep.py": "modified", "evil": "modified"}


@pytest.fixture
def repo():
    d = tempfile.mkdtemp()
    pathlib.Path(d, "ok.txt").write_text("hello inside\n")
    pathlib.Path(d, "sub").mkdir()
    pathlib.Path(d, "sub", "deep.py").write_text("import os\n")
    os.symlink("/etc/passwd", os.path.join(d, "evil"))
    return d


def test_reads_inside(repo):
    assert read_file(repo_root=repo, scope=OPEN_SCOPE, path="ok.txt") == "hello inside\n"


@pytest.mark.parametrize(
    "path", ["../../../../etc/passwd", "/etc/passwd", "sub/../../..", "evil"]
)
def test_escape_refused(repo, path):
    assert read_file(repo_root=repo, scope=OPEN_SCOPE, path=path).startswith(
        "error: path escapes"
    )


def test_symlink_in_scope_is_not_followed(repo):
    """`evil` is listed in scope and still yields nothing from outside."""
    out = search_code(repo_root=repo, scope=OPEN_SCOPE, pattern="root")
    assert "/etc/passwd" not in out
    assert "0 hits" in out


def test_search_escape_refused(repo):
    assert search_code(
        repo_root=repo, scope=OPEN_SCOPE, pattern="root", path="../.."
    ).startswith("error: path escapes")
