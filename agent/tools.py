"""Tools the model may call, and the two jails they enforce.

Jail 1 - the path jail (`_resolve`): nothing outside the checkout. Security.
Model-chosen calls run over untrusted third-party code.

Jail 2 - the scope jail (`_scope_error`): nothing outside this diff. Budget.
A live 2-file PR spent all ten iterations searching the repository for symbols
that were not in the diff, and never once read a changed file. The prompt said
stay in scope; the tools let it leave, so leaving felt productive. Saying it
twice does not work. The tools have to be the authority.

Order matters: the path jail is checked first, so a traversal attempt is
reported as a traversal attempt, not as an out-of-scope path.
"""

import re
from collections.abc import Mapping
from pathlib import Path

MAX_READ_BYTES = 60 * 1024
MAX_SEARCH_HITS = 50

# At most this many in-scope paths are echoed in a refusal. A 60-file PR would
# otherwise re-inflate the context on every refused call.
MAX_SCOPE_LISTED = 10

ESCAPE = "error: path escapes the repository root and was refused"

NOT_IN_DIFF = (
    "error: {path} is not part of this diff. In scope: {listing}. "
    "Read one of these, or output your findings JSON now."
)

DELETED = "error: {path} was deleted by this diff; there is nothing to read."

Scope = Mapping[str, str]


def _err(exc: BaseException) -> str:
    return f"error: {type(exc).__name__}: {exc}"


# -- jail 1: the path jail -------------------------------------------------


def _resolve(repo_root: str, path: str) -> Path | None:
    """Resolve `path` inside `repo_root`. None if it escapes the jail.

    Both sides are resolved (following symlinks) before the containment check,
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


def _relative(repo_root: str, target: Path) -> str:
    """Canonical repo-relative form, so `./a.py` and `a.py` hit the same key."""
    return str(target.relative_to(Path(repo_root).resolve()))


# -- jail 2: the scope jail ------------------------------------------------


def _readable(scope: Scope) -> list[str]:
    """Changed paths that still exist on disk. A deleted file is in the diff
    but not in the checkout, so it is in scope for reasoning, not for reading."""
    return [path for path, status in scope.items() if status != "deleted"]


def _listing(scope: Scope) -> str:
    paths = _readable(scope)
    shown = ", ".join(paths[:MAX_SCOPE_LISTED])
    hidden = len(paths) - MAX_SCOPE_LISTED
    if not shown:
        return "(no readable files in this diff)"
    return f"{shown} (+{hidden} more)" if hidden > 0 else shown


def _scope_error(rel: str, scope: Scope) -> str | None:
    """The refusal for `rel`, or None if it is in scope and readable.

    The refusal names the in-scope paths on purpose. A bare "not found" reads
    like a normal empty answer and the model tries another guess; naming the
    alternatives turns a dead end into a redirect.
    """
    status = scope.get(rel)
    if status is None:
        return NOT_IN_DIFF.format(path=rel, listing=_listing(scope))
    if status == "deleted":
        return DELETED.format(path=rel)
    return None


# -- tools -----------------------------------------------------------------


def read_file(*, repo_root: str, scope: Scope, path: str) -> str:
    target = _resolve(repo_root, path)
    if target is None:
        return ESCAPE
    rel = _relative(repo_root, target)
    refusal = _scope_error(rel, scope)
    if refusal is not None:
        return refusal
    try:
        raw = target.read_bytes()
    except Exception as e:
        return _err(e)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return _err(e)
    if len(text.encode("utf-8")) <= MAX_READ_BYTES:
        return text
    return _truncate(text)


def _truncate(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    size = 0
    for line in lines:
        b = len(line.encode("utf-8"))
        if out and size + b > MAX_READ_BYTES:
            break
        out.append(line)
        size += b
    return "".join(out) + f"\n[truncated: {len(lines) - len(out)} more lines]"


def search_code(*, repo_root: str, scope: Scope, pattern: str, path: str = ".") -> str:
    """Regex over the changed files only.

    Scoping the search rather than refusing it keeps the tool useful on a wide
    PR - reading every changed file can exhaust the tool-bytes budget on its
    own - while removing the affordance that let the agent audit the whole
    repository against rules that were never part of the change.
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return _err(e)

    root = Path(repo_root).resolve()
    targets = _readable(scope)

    if path not in ("", "."):
        resolved = _resolve(repo_root, path)
        if resolved is None:
            return ESCAPE
        rel = _relative(repo_root, resolved)
        refusal = _scope_error(rel, scope)
        if refusal is not None:
            return refusal
        targets = [rel]

    hits: list[str] = []
    for rel in targets:
        candidate = root / rel
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            content = candidate.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{number}:{line}")
                if len(hits) >= MAX_SEARCH_HITS:
                    return _report(len(targets), hits, capped=True)
    return _report(len(targets), hits)


def _report(searched: int, hits: list[str], *, capped: bool = False) -> str:
    """Always states what was searched, so zero hits cannot read as an answer."""
    files = "file" if searched == 1 else "files"
    plural = "hit" if len(hits) == 1 else "hits"
    header = f"searched {searched} changed {files}, {len(hits)} {plural}"
    if capped:
        header += f" (capped at {MAX_SEARCH_HITS})"
    return header + "\n" + "\n".join(hits) if hits else header


# -- budget signalling -----------------------------------------------------


def is_unproductive(result: str) -> bool:
    """True for a result that taught the model nothing: error, refusal, no hits.

    Four of the ten iterations in the run that motivated the scope jail
    returned an empty string. An empty tool result looks exactly like a
    successful one, so the budget drained without anything appearing wrong.
    `execute_tools` appends the remaining iteration count to these.

    The string knowledge lives here, next to the strings themselves, so the
    graph node does not have to know how a refusal is spelled.
    """
    if not result.strip():
        return True
    if result.startswith("error:"):
        return True
    return result.startswith("searched ") and ", 0 hits" in result


# -- schemas ---------------------------------------------------------------
#
# There is deliberately no `list_files`. Restricted to the diff it would only
# echo the manifest already in the first message; unrestricted it is the hole
# the scope jail exists to close. It was iteration 1 of the run that failed.

TOOL_SCHEMAS = [
    {
        "name": "read_file",
        "description": (
            "Read a file changed by this pull request. Only paths listed in "
            "CHANGED FILES can be read; anything else is refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to repo root, from CHANGED FILES.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search_code",
        "description": (
            "Regex search across the files changed by this pull request. "
            "Does not search the rest of the repository."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional: restrict to one changed file.",
                },
            },
            "required": ["pattern"],
        },
    },
]

DISPATCH = {
    "read_file": read_file,
    "search_code": search_code,
}
