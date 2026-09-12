from dataclasses import dataclass
import re

# Group 1 is the post-image start line, group 2 its length (absent means 1).
# `post_images` only needs the start; `hunk_ranges` needs both, and one regex
# for both keeps the two readings of a hunk header from drifting apart.
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class PostImage:
    path: str
    text: str
    line_numbers: tuple[int | None, ...]

    def locate(self, evidence: str) -> int | None:
        start = 0
        while True:
            index = self.text.find(evidence, start)
            if index < 0:
                return None
            line_index = self.text.count("\n", 0, index)
            # post_images() builds lines from splitlines(), so a line never
            # contains "\n" and this index is in range. The guard makes the
            # invariant explicit rather than load-bearing and unstated: any
            # other constructor of PostImage would crash here instead.
            if line_index >= len(self.line_numbers):
                return None
            line = self.line_numbers[line_index]
            if line is not None:
                return line
            start = index + 1


def post_images(diff: str) -> list[PostImage]:
    """Reconstruct visible post-image hunk lines and their right-side numbers."""
    files: dict[str, tuple[list[str], list[int | None]]] = {}
    path: str | None = None
    new_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
            in_hunk = False
            continue
        if raw.startswith("+++ "):
            path = _plus_path(raw)
            in_hunk = False
            if path is not None:
                files.setdefault(path, ([], []))
            continue
        if raw.startswith("@@ "):
            match = HUNK_RE.match(raw)
            if match is None or path is None:
                in_hunk = False
                continue
            lines, numbers = files[path]
            if lines:
                lines.append("")
                numbers.append(None)
            new_line = int(match.group(1))
            in_hunk = True
            continue
        if not in_hunk or path is None:
            continue
        if raw.startswith(("+", " ")):
            lines, numbers = files[path]
            lines.append(raw[1:])
            numbers.append(new_line)
            new_line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            in_hunk = False

    return [
        PostImage(path, "\n".join(lines), tuple(numbers))
        for path, (lines, numbers) in files.items()
        if lines
    ]


def hunk_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """`{path: [(first_line, last_line), ...]}` in post-image numbering.

    Where in each changed file the change actually is. `read_file` uses it to
    open a file that is too large to return whole AROUND its hunks, rather
    than from byte zero: `store/codec.py` is 2,741 lines of which the diff
    touches 2724-2741, and a head-truncating read returned 1,609 lines of
    generated lookup table and none of the changed code.

    Same path convention as `changed_paths`, so the keys line up with `scope`.
    A deleted file has no post-image and is absent, as is a zero-length hunk -
    a pure deletion covers no line that exists to be read.
    """
    out: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            path = None
            continue
        if raw.startswith("+++ "):
            path = _plus_path(raw)
            continue
        if not raw.startswith("@@ ") or path is None:
            continue
        match = HUNK_RE.match(raw)
        if match is None:
            continue
        start = int(match.group(1))
        length = int(match.group(2)) if match.group(2) is not None else 1
        if length <= 0:
            continue
        out.setdefault(path, []).append((start, start + length - 1))
    return out


def changed_paths(diff: str) -> dict[str, str]:
    """`{path: "added" | "modified" | "deleted"}`, straight from the diff headers.

    The single source of truth for what this PR touches. Two things read it:
    the manifest the model is shown, and the scope jail the tools enforce.
    They must never disagree - a second parser would eventually drift, and the
    symptom would be a changed file the agent is refused permission to read.

    Insertion order follows the diff, so the manifest is stable across runs.
    """
    out: dict[str, str] = {}
    lines = diff.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("+++ "):
            continue
        new = line[4:].strip()
        old = lines[i - 1][4:].strip() if i and lines[i - 1].startswith("--- ") else ""
        if new == "/dev/null":
            out[_strip_prefix(old)] = "deleted"
        else:
            out[_strip_prefix(new)] = "added" if old == "/dev/null" else "modified"
    return out


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _plus_path(plus_line: str) -> str | None:
    path = plus_line[4:]
    if path.startswith("b/"):
        path = path[2:]
    return None if path == "/dev/null" else path
