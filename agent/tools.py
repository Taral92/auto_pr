import re
from pathlib import Path

IGNORE_DIRS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}
LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
}
MAX_LIST_FILES = 500
MAX_READ_BYTES = 60 * 1024
MAX_SEARCH_HITS = 50


def _err(exc: BaseException) -> str:
    return f"error: {type(exc).__name__}: {exc}"


def _resolve(repo_root: str, path: str) -> Path | None:
    """Resolve `path` inside `repo_root`. None if it escapes the jail.

    H1: both sides resolved (follows symlinks) before the containment check,
    so a symlink pointing outside the repo is rejected, not followed.
    """
    root = Path(repo_root).resolve()
    try:
        target = (root / path).resolve()
    except (OSError, RuntimeError):
        return None
    if target != root and root not in target.parents:
        return None
    return target


ESCAPE = "error: path escapes the repository root and was refused"


def _ignored(path: Path) -> bool:
    if any(part in IGNORE_DIRS for part in path.parts):
        return True
    name = path.name
    return name.endswith(".lock") or name in LOCKFILE_NAMES


def list_files(*, repo_root: str, path: str = ".") -> str:
    try:
        root = Path(repo_root).resolve()
        target = _resolve(repo_root, path)
        if target is None:
            return ESCAPE
        if not target.exists():
            return f"error: path not found: {path}"
        names: list[str] = []
        for p in target.rglob("*"):
            if _ignored(p) or p.is_symlink():
                continue
            if p.is_file():
                names.append(str(p.relative_to(root)))
            if len(names) >= MAX_LIST_FILES:
                names.append(f"[truncated: listing capped at {MAX_LIST_FILES} files]")
                break
        return "\n".join(names)
    except Exception as e:
        return _err(e)


def read_file(*, repo_root: str, path: str) -> str:
    target = _resolve(repo_root, path)
    if target is None:
        return ESCAPE
    try:
        raw = target.read_bytes()
    except Exception as e:
        return _err(e)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return _err(e)
    encoded_len = len(text.encode("utf-8"))
    if encoded_len <= MAX_READ_BYTES:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    size = 0
    for line in lines:
        b = len(line.encode("utf-8"))
        if out and size + b > MAX_READ_BYTES:
            break
        out.append(line)
        size += b
    more = len(lines) - len(out)
    return "".join(out) + f"\n[truncated: {more} more lines]"


def search_code(*, repo_root: str, pattern: str, path: str = ".") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return _err(e)
    try:
        root = Path(repo_root).resolve()
        target = _resolve(repo_root, path)
        if target is None:
            return ESCAPE
        if not target.exists():
            return f"error: path not found: {path}"
        hits: list[str] = []
        files = [target] if target.is_file() else target.rglob("*")
        for p in files:
            if not p.is_file() or _ignored(p) or p.is_symlink():
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = p.relative_to(root)
            for i, line in enumerate(content.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{i}:{line}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        hits.append(f"[truncated: search capped at {MAX_SEARCH_HITS} hits]")
                        return "\n".join(hits)
        return "\n".join(hits)
    except Exception as e:
        return _err(e)


TOOL_SCHEMAS = [
    {
        "name": "list_files",
        "description": "List files under a directory in the repo, relative to repo root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to repo root. Defaults to the repo root.",
                },
            },
        },
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a file in the repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to repo root.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": "Keyword/regex search over file contents in the repo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file relative to repo root. Defaults to the repo root.",
                },
            },
            "required": ["pattern"],
        },
    },
]

DISPATCH = {
    "list_files": list_files,
    "read_file": read_file,
    "search_code": search_code,
}
