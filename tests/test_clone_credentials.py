"""The installation token must never land in the agent's workspace.

`agent/review.py` clones into a temp dir and then hands that same path to the
graph as `workspace` - the tree the model's jailed tools read. The token used to
travel in the remote URL, so `git remote add` wrote it into
`<workspace>/.git/config`. The scope jail refused that path, so it was never
reachable, but a credential with write access to pull requests should not be
inside the sandbox at all: it made one jail bug the difference between reading
an unrelated file and leaking a live token.

These tests drive the real `clone_head` against a LOCAL git repository. No
network and no GitHub: the fetch URL is rewritten to a local path, which is
enough to prove the credential never reaches the workspace while fetch and
checkout still work.
"""

import base64
import os
import subprocess
import sys

import pytest

from core.errors import PermanentError, TransientError
from gh import clone as C

TOKEN = "ghs_SUPERSECRET_TOKEN_VALUE_0123456789"


def _git(*args, cwd, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, **(env or {}), "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


@pytest.fixture
def remote(tmp_path):
    """A local directory laid out like the host, so `GITHUB` can point at it.

    `clone_head` builds its URL as `f"{GITHUB}{owner}/{repo}.git"`, so pointing
    GITHUB at `<tmp>/remote/` makes the real construction resolve to a real
    path. Nothing about the production code path is stubbed.
    """
    src = tmp_path / "remote" / "o" / "r.git"
    src.mkdir(parents=True)
    _git("init", "-q", ".", cwd=src)
    _git("config", "user.email", "t@t", cwd=src)
    _git("config", "user.name", "t", cwd=src)
    (src / "app.py").write_text("def handler():\n    return 1\n")
    _git("add", "-A", cwd=src)
    _git("commit", "-qm", "init", cwd=src)
    _git("update-ref", "refs/pull/7/head", "HEAD", cwd=src)
    return src, _git("rev-parse", "HEAD", cwd=src).strip()


@pytest.fixture
def host(remote, monkeypatch, tmp_path):
    monkeypatch.setattr(C, "GITHUB", f"{tmp_path / 'remote'}/")
    return remote


@pytest.fixture
def cloned(tmp_path, host):
    """The real clone_head, start to finish, against the local host."""
    _src, sha = host
    dest = tmp_path / "work"
    dest.mkdir()
    C.clone_head(str(dest), "o", "r", 7, TOKEN, sha)
    return dest, sha


def _capture(monkeypatch):
    """Record every git argv and env while letting the command really run."""
    calls: list[tuple[list[str], dict]] = []
    real = subprocess.run

    def fake_run(cmd, **kw):
        calls.append(([str(a) for a in cmd], kw.get("env") or {}))
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# -- it still works --------------------------------------------------------


def test_the_clone_still_checks_out_the_head_sha(cloned):
    dest, sha = cloned
    assert _git("rev-parse", "HEAD", cwd=dest).strip() == sha


def test_the_working_tree_is_populated(cloned):
    dest, _ = cloned
    assert (dest / "app.py").read_text() == "def handler():\n    return 1\n"


def test_a_moved_head_is_still_refused(tmp_path, host):
    """The PR-moved-mid-run guard must survive the credential change."""
    dest = tmp_path / "moved"
    dest.mkdir()
    with pytest.raises(PermanentError, match="PR moved mid-run"):
        C.clone_head(str(dest), "o", "r", 7, TOKEN, "0" * 40)


# -- the token is not in the workspace ------------------------------------


def _all_bytes(root) -> bytes:
    """Every byte of every file under the workspace, .git included."""
    blob = bytearray()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            blob += name.encode("utf-8", "surrogateescape")
            try:
                blob += (os.path.join(dirpath, name) and
                         open(os.path.join(dirpath, name), "rb").read())
            except OSError:
                pass
    return bytes(blob)


def test_the_token_is_absent_from_git_config(cloned):
    dest, _ = cloned
    config = (dest / ".git" / "config").read_text()
    assert TOKEN not in config
    assert "x-access-token" not in config
    assert "extraHeader" not in config          # not persisted either


def test_no_remote_url_is_configured_at_all(cloned):
    dest, _ = cloned
    assert _git("remote", "-v", cwd=dest).strip() == ""


def test_the_token_is_absent_from_every_file_in_the_workspace(cloned):
    """Includes .git/FETCH_HEAD, reflogs, packed refs - everything."""
    dest, _ = cloned
    blob = _all_bytes(dest)
    assert TOKEN.encode() not in blob
    b64 = base64.b64encode(f"x-access-token:{TOKEN}".encode())
    assert b64 not in blob
    assert b"x-access-token" not in blob


def test_fetch_head_records_no_credential(cloned):
    dest, _ = cloned
    fetch_head = dest / ".git" / "FETCH_HEAD"
    if fetch_head.exists():
        assert TOKEN not in fetch_head.read_text()
        assert "x-access-token" not in fetch_head.read_text()


# -- the token is not in argv ---------------------------------------------


def test_the_token_never_appears_in_a_command_line(tmp_path, host, monkeypatch):
    """argv is world-readable in a process listing."""
    _src, sha = host
    calls = _capture(monkeypatch)
    dest = tmp_path / "argv"
    dest.mkdir()
    C.clone_head(str(dest), "o", "r", 7, TOKEN, sha)

    assert calls, "no git commands ran"
    for cmd, _env in calls:
        joined = " ".join(cmd)
        assert TOKEN not in joined
        assert "x-access-token" not in joined
        assert "Authorization" not in joined


def test_the_credential_is_passed_through_the_environment(tmp_path, host, monkeypatch):
    """Proves the replacement mechanism is in use, scoped to the host."""
    _src, sha = host
    calls = _capture(monkeypatch)
    dest = tmp_path / "env"
    dest.mkdir()
    C.clone_head(str(dest), "o", "r", 7, TOKEN, sha)

    _cmd, env = calls[0]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"].startswith("http.")
    assert env["GIT_CONFIG_KEY_0"].endswith(".extraHeader")
    expected = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {expected}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_the_auth_header_is_scoped_to_one_host():
    """An unscoped extraHeader would follow a redirect to another host."""
    key = C._auth_env("t")["GIT_CONFIG_KEY_0"]
    assert key == "http.https://github.com/.extraHeader"


def test_git_timeout_is_transient_and_fetch_is_bounded(tmp_path, monkeypatch):
    dest = tmp_path / "timeout"
    dest.mkdir()
    calls = []

    def timed_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    monkeypatch.setattr(C.subprocess, "run", timed_run)
    with pytest.raises(TransientError, match="git fetch timed out"):
        C.clone_head(str(dest), "o", "r", 7, TOKEN, "0" * 40)

    fetch = next(kwargs for cmd, kwargs in calls if "fetch" in cmd)
    assert fetch["timeout"] == 120


# -- the token is not in logs or errors -----------------------------------


def test_a_git_failure_does_not_echo_the_token(tmp_path, monkeypatch):
    dest = tmp_path / "fail"
    dest.mkdir()
    with pytest.raises((TransientError, PermanentError)) as excinfo:
        C.clone_head(str(dest), "o", "r", 7, TOKEN, "0" * 40)
    message = str(excinfo.value)
    assert TOKEN not in message
    assert "x-access-token" not in message
    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() not in message


def test_redact_still_scrubs_a_token_it_is_handed():
    """Kept as belt-and-braces even though the token no longer enters a URL."""
    assert TOKEN not in C.redact(f"fatal: auth failed for {TOKEN}", TOKEN)
    assert C.redact("https://x-access-token:abc@github.com/o/r", "") == (
        "https://***@github.com/o/r")


def test_nothing_is_printed_during_a_successful_clone(cloned, capsys):
    out = capsys.readouterr()
    assert TOKEN not in out.out and TOKEN not in out.err


# -- cleanup leaves nothing behind ---------------------------------------


def test_rmtree_removes_the_workspace_and_its_credentials(cloned):
    dest, _ = cloned
    assert (dest / ".git").exists()
    C.rmtree(str(dest))
    assert not os.path.exists(dest)


def test_rmtree_raises_if_the_workspace_survives(tmp_path, monkeypatch):
    """A workspace left behind is a potential artifact leak; it must be loud."""
    target = tmp_path / "stuck"
    target.mkdir()
    monkeypatch.setattr(C.shutil, "rmtree", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="temp dir still present"):
        C.rmtree(str(target))
