import base64
import os
import re
import shutil
import stat
import subprocess
from urllib.parse import quote

from core.errors import PermanentError, TransientError


#: The header host scope. Narrow on purpose: an unscoped `http.extraHeader`
#: would be sent to whatever host a redirect pointed at.
GITHUB = "https://github.com/"


def _auth_env(token: str) -> dict[str, str]:
    """Git config handed over through the environment, never the workspace.

    The token used to travel in the remote URL - `git remote add origin
    https://x-access-token:TOKEN@github.com/...` - which git then wrote into
    `<workspace>/.git/config`. That workspace is exactly the tree the model's
    jailed tools read. The scope jail refused `.git/config`, so it was not
    reachable, but a credential with write access to pull requests should not be
    sitting inside the sandbox at all: it made one scope-jail bug the difference
    between reading an unrelated file and leaking a live token.

    `GIT_CONFIG_COUNT`/`_KEY_`/`_VALUE_` give git the same setting for the life
    of one process. Nothing is persisted, and the value never appears in argv,
    so it cannot be lifted out of a process listing either.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{GITHUB}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def clone_head(
    dest: str, owner: str, repo: str, number: int, token: str, head_sha: str
) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_auth_env(token)}
    # No credentials in this URL, and no `remote add` at all - a configured
    # remote is what persisted the token. `fetch <url>` leaves nothing behind
    # but FETCH_HEAD, which records this token-free URL.
    # Built from the same constant the auth header is scoped to, so the two
    # cannot drift: a header scoped to a different host than the URL would
    # simply not be sent, and the fetch would fail as unauthenticated.
    origin = f"{GITHUB}{owner}/{repo}.git"
    helper_off = ["-c", "credential.helper="]

    def git(args: list[str]) -> str:
        cmd = ["git", "-C", dest, *helper_off, *args]
        try:
            proc = subprocess.run(
                cmd, check=True, capture_output=True, text=True, env=env
            )
            return proc.stdout
        except subprocess.CalledProcessError as e:
            msg = redact((e.stderr or e.stdout or "").strip(), token)
            lower = msg.lower()
            if "authentication" in lower or "could not read username" in lower:
                raise PermanentError(f"git {args[0]} failed: {msg}") from None
            raise TransientError(f"git {args[0]} failed: {msg}") from None

    git(["init"])
    git(["fetch", "--depth=1", origin, f"pull/{number}/head"])
    git(["checkout", "FETCH_HEAD"])
    got = git(["rev-parse", "HEAD"]).strip()
    if got != head_sha:
        raise PermanentError(
            f"HEAD {got} != head.sha {head_sha}; PR moved mid-run"
        )


def redact(text: str, token: str) -> str:
    if not text:
        return text
    out = text
    if token:
        out = out.replace(token, "***")
        out = out.replace(quote(token, safe=""), "***")
    out = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", out)
    out = re.sub(r"https://[^@\s]+@github\.com", "https://***@github.com", out)
    return out


def rmtree(path: str) -> None:
    def onerror(func, p, _exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    shutil.rmtree(path, onerror=onerror)
    if os.path.exists(path):
        raise RuntimeError(f"temp dir still present: {path}")
