import os
import pathlib
import tempfile

import pytest

from agent.tools import list_files, read_file, search_code


@pytest.fixture
def repo():
    d = tempfile.mkdtemp()
    pathlib.Path(d, "ok.txt").write_text("hello inside\n")
    pathlib.Path(d, "sub").mkdir()
    pathlib.Path(d, "sub", "deep.py").write_text("import os\n")
    os.symlink("/etc/passwd", os.path.join(d, "evil"))
    return d


def test_reads_inside(repo):
    assert read_file(repo_root=repo, path="ok.txt") == "hello inside\n"


@pytest.mark.parametrize(
    "path", ["../../../../etc/passwd", "/etc/passwd", "sub/../../..", "evil"]
)
def test_escape_refused(repo, path):
    assert read_file(repo_root=repo, path=path).startswith("error: path escapes")


def test_symlink_not_listed(repo):
    assert "evil" not in list_files(repo_root=repo, path=".")


def test_search_escape_refused(repo):
    assert search_code(repo_root=repo, pattern="root", path="../..").startswith(
        "error: path escapes"
    )
