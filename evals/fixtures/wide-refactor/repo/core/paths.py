"""Path containment helpers."""

from pathlib import Path


def _resolve(root: str, path: str) -> Path | None:
    base = Path(root).resolve()
    try:
        target = (base / path).resolve()
    except (OSError, RuntimeError):
        return None
    if target != base and base not in target.parents:
        return None
    return target


def relative(root: str, target: Path) -> str:
    return str(target.relative_to(Path(root).resolve()))
