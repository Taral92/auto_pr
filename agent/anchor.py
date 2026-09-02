import re
from collections import defaultdict

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_anchorable(diff: str) -> dict[str, set[int]]:
    """Map each path to the set of RIGHT-side line numbers GitHub will accept."""
    anchorable: dict[str, set[int]] = defaultdict(set)
    path: str | None = None
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            path = _plus_path(raw)
            in_hunk = False
            continue
        if raw.startswith("@@ "):
            m = HUNK_RE.match(raw)
            if not m:
                in_hunk = False
                continue
            new_line = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk or path is None:
            continue
        if raw.startswith("+") or raw.startswith(" "):
            anchorable[path].add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            pass
        elif raw.startswith("\\"):
            pass
        else:
            in_hunk = False
    return dict(anchorable)


def locate_in_diff(diff: str, evidence: str) -> tuple[str, int] | None:
    """Return (path, RIGHT-side line) for `evidence`, skipping `-` lines."""
    start = 0
    while True:
        idx = diff.find(evidence, start)
        if idx < 0:
            return None
        loc = _right_line_at(diff, idx)
        if loc is not None:
            return loc
        start = idx + 1


def _right_line_at(diff: str, idx: int) -> tuple[str, int] | None:
    path: str | None = None
    new_line = 0
    in_hunk = False
    offset = 0
    for line in diff.splitlines(keepends=True):
        raw = line[:-1] if line.endswith("\n") else line
        end = offset + len(line)
        hit = offset <= idx < end
        if raw.startswith("+++ "):
            path = _plus_path(raw)
            in_hunk = False
        elif raw.startswith("@@ "):
            m = HUNK_RE.match(raw)
            if m:
                new_line = int(m.group(1))
                in_hunk = True
            else:
                in_hunk = False
        elif in_hunk and path is not None:
            if raw.startswith("+") or raw.startswith(" "):
                if hit:
                    return (path, new_line)
                new_line += 1
            elif raw.startswith("-"):
                if hit:
                    return None
            elif not raw.startswith("\\"):
                in_hunk = False
        if hit:
            return None
        offset = end
    return None


def _plus_path(plus_line: str) -> str | None:
    path = plus_line[4:]
    if path.startswith("b/"):
        path = path[2:]
    if path == "/dev/null":
        return None
    return path
