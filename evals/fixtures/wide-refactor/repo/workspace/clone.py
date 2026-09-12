import shutil
import tempfile
from pathlib import Path


def checkout(source: str) -> str:
    tmp = tempfile.mkdtemp(prefix="ws-")
    target = Path(tmp) / "repo"
    shutil.copytree(source, target)
    return str(target)
