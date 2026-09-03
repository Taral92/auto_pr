from dataclasses import dataclass
import re

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


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


def _plus_path(plus_line: str) -> str | None:
    path = plus_line[4:]
    if path.startswith("b/"):
        path = path[2:]
    return None if path == "/dev/null" else path
