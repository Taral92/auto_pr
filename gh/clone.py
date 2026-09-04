import os
import re
import shutil
import stat
import subprocess
from urllib.parse import quote

from core.errors import PermanentError, TransientError


def clone_head(
    dest: str, owner: str, repo: str, number: int, token: str, head_sha: str
) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    origin = (
        f"https://x-access-token:{quote(token, safe='')}@github.com/{owner}/{repo}.git"
    )
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
    git(["remote", "add", "origin", origin])
    git(["fetch", "--depth=1", "origin", f"pull/{number}/head"])
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
