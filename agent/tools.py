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

from core.models import ReviewFindings

MAX_READ_BYTES = 60 * 1024
MAX_SEARCH_HITS = 50

#: Lines kept either side of a hunk when a file is too big to return whole.
#: Generous on purpose: the changed lines are already in the diff, so what a
#: read has to supply is what surrounds them - the imports, the rest of the
#: class, the neighbouring function that shares the invariant.
HUNK_CONTEXT_LINES = 80

OMITTED = (
    "[lines {first}-{last} omitted ({count} lines): not changed by this diff. "
    "search_code reaches them.]"
)

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


def read_file(
    *,
    repo_root: str,
    scope: Scope,
    path: str,
    hunks: Mapping[str, list[tuple[int, int]]] | None = None,
) -> str:
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
    # Too big to return whole. Open it around the change instead of from the
    # top: head-truncating this file returned 1,609 lines of generated table
    # and cut off the only code the diff touched.
    windows = _windows((hunks or {}).get(rel), len(text.splitlines()))
    if not windows:
        return _truncate(text)          # nothing to centre on; old behaviour
    return _elide(text, windows)


def _windows(
    ranges: list[tuple[int, int]] | None, total: int
) -> list[tuple[int, int]]:
    """Hunk ranges widened by context and merged where they meet.

    Merging is what stops two hunks a few lines apart from producing an
    elision marker announcing that nothing was elided.
    """
    if not ranges:
        return []
    # A range starting past the end of the file means the diff and the
    # checkout disagree. Drop it rather than emit an empty window.
    widened = sorted(
        (max(1, start - HUNK_CONTEXT_LINES), min(total, end + HUNK_CONTEXT_LINES))
        for start, end in ranges
        if start <= total
    )
    merged: list[tuple[int, int]] = []
    for start, end in widened:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _elide(text: str, windows: list[tuple[int, int]]) -> str:
    """The windows verbatim, with each gap replaced by a marker naming it.

    The kept lines are copied exactly and carry no line-number prefix, because
    a finding's evidence has to be a character-for-character substring of this
    text to survive grounding. A `1234: ` prefix would make every quote fail.
    """
    lines = text.splitlines()
    total = len(lines)
    parts: list[str] = []
    cursor = 1
    for start, end in windows:
        if start > cursor:
            parts.append(
                OMITTED.format(first=cursor, last=start - 1, count=start - cursor)
            )
        parts.append("\n".join(lines[start - 1 : end]))
        cursor = end + 1
    if cursor <= total:
        parts.append(
            OMITTED.format(first=cursor, last=total, count=total - cursor + 1)
        )
    out = "\n".join(parts)
    # Backstop: one pathological hunk can still be bigger than the cap.
    if len(out.encode("utf-8")) > MAX_READ_BYTES:
        return _truncate(out)
    return out


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


def search_code(
    *,
    repo_root: str,
    scope: Scope,
    pattern: str,
    path: str = ".",
    hunks: Mapping[str, list[tuple[int, int]]] | None = None,
) -> str:
    """Regex over the changed files only.

    `hunks` is accepted and unused, so every tool in DISPATCH takes the same
    keywords. Searching is deliberately NOT restricted to the hunks: it is the
    way to reach the part of a large file that `read_file` elided.

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


#: A line this tool added to describe what it left out, rather than a line of
#: the file. Anchored end to end so a source line that merely starts with "["
#: is not mistaken for one.
_MARKER_RE = re.compile(
    r"^\[lines \d+-\d+ omitted \(\d+ lines?\):.*\]$"
    r"|^\[truncated: \d+ more lines?\]$"
)


def evidence_segments(result: str) -> list[str]:
    """The runs of verbatim source in a tool result, with metadata removed.

    The corpus is what a finding's evidence is matched against, so anything
    in it is quotable as proof. An elision marker is not code under review -
    it is this module talking about the code - and `[lines 1-2643 omitted
    (2643 lines): ...]` clears the 20-character evidence minimum comfortably.
    The marker stays in the message, where it does its job of telling the
    model what it has not been shown; it just never becomes evidence.

    Splitting rather than deleting matters. Deleting the marker would butt
    line 2643 against line 2644 and manufacture a contiguous span that does
    not exist in the file - closing one hole by opening a subtler one. Each
    window is its own segment, so evidence cannot bridge the gap either.

    A result with no markers is returned untouched, byte for byte, so nothing
    about grounding on ordinary reads and searches changes.
    """
    lines = result.splitlines()
    if not any(_MARKER_RE.match(line) for line in lines):
        return [result]
    segments: list[str] = []
    current: list[str] = []
    for line in lines:
        if _MARKER_RE.match(line):
            if current:
                segments.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        segments.append("\n".join(current))
    return [segment for segment in segments if segment.strip()]


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

SUBMIT_FINDINGS = "submit_findings"

#: The completion tool. It is the review's exit, not a filesystem operation,
#: so it is deliberately absent from DISPATCH - `execute_tools` intercepts it.
#:
#: Completion used to be inferred from the ABSENCE of a tool call, which made
#: "the model is finished" and "the model wandered off protocol" the same
#: event, and left a brace-matching parser plus an untracked repair call to
#: sort out the difference. Declaring completion through a schema the API
#: itself validates removes both.
SUBMIT_SCHEMA = {
    "name": SUBMIT_FINDINGS,
    "description": (
        "Report your review and END it. Call this once you have enough "
        "evidence - it is the only way to finish. Every finding's `evidence` "
        "must be copied character for character out of the diff or a tool "
        "result. An empty findings list is a valid, correct review of code "
        "with no defects."
    ),
    "input_schema": ReviewFindings.model_json_schema(),
}

TOOL_SCHEMAS = [
    SUBMIT_SCHEMA,
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
