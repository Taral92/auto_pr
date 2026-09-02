import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agent.loop import run


def main() -> None:
    repo_root = os.environ["REPO_PATH"]
    diff_path = ROOT / "fixtures" / "pr-001.diff"
    diff = diff_path.read_text()

    trace: list = []
    findings = None
    error = None
    try:
        findings = run(diff=diff, repo_root=repo_root, trace=trace)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        runs_dir = ROOT / "runs"
        runs_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        out = runs_dir / f"{ts}.json"
        payload = {
            "timestamp": ts,
            "repo_root": repo_root,
            "diff_path": str(diff_path),
            "model": os.environ.get("ANTHROPIC_MODEL") or os.environ.get("MODEL"),
            "steps": trace,
            "findings": findings.model_dump() if findings is not None else None,
            "error": error,
        }
        out.write_text(json.dumps(payload, indent=2))
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
