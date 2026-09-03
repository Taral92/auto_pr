import fnmatch
import json
import shutil
import subprocess
from pathlib import Path

import config
from agent import nodes
from agent.local import review_local
from agent.model_client import _Response
from evals.runner import FIXTURES, load_cases

ORIGINAL_FILES = '''"""File access helpers for the plugin runtime."""

import re
from pathlib import Path

MAX_READ_BYTES = 64 * 1024


def _inside(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def read_file(*, root: str, path: str) -> str:
    base = Path(root).resolve()
    target = (base / path).resolve()
    if not _inside(base, target):
        raise PermissionError(f"refused: {path}")
    return target.read_text()[:MAX_READ_BYTES]


def search(*, root: str, pattern: str) -> list[str]:
    rx = re.compile(pattern)
    hits = []
    for p in Path(root).rglob("*.py"):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if rx.search(line):
                hits.append(f"{p}:{i}:{line}")
    return hits
'''


def _response(stop_reason: str, content: list[dict]) -> _Response:
    return _Response(
        {
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "content": content,
        }
    )


def test_load_cases_from_fixture_directories():
    case = next(c for c in load_cases(None) if c["id"] == "sandbox-escape")

    assert Path(case["repo_dir"]) == FIXTURES / "sandbox-escape" / "repo"
    assert "diff --git a/app/files.py" in case["diff"]
    assert len(case["expected"]) == 2


def test_fixture_repos_contain_no_eval_data():
    repos = sorted(FIXTURES.glob("*/repo"))
    assert repos

    contaminated = []
    for repo in repos:
        for path in repo.rglob("*"):
            name = path.name.lower()
            if path.is_file() and (
                fnmatch.fnmatch(name, "*expected*")
                or fnmatch.fnmatch(name, "*eval*")
            ):
                contaminated.append(path.relative_to(repo))

    assert contaminated == []


def test_fixture_repo_is_post_image_of_diff(tmp_path):
    fixture = FIXTURES / "sandbox-escape"
    repo = tmp_path / "repo"
    shutil.copytree(fixture / "repo", repo)

    subprocess.run(
        ["git", "apply", "--reverse", str(fixture / "head.diff")],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    assert (repo / "app" / "files.py").read_text() == ORIGINAL_FILES


def test_review_local_runs_without_accessing_evals(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    config.get_settings.cache_clear()
    responses = iter(
        [
            _response(
                "tool_use",
                [
                    {
                        "type": "tool_use",
                        "id": "list-1",
                        "name": "list_files",
                        "input": {"path": "."},
                    }
                ],
            ),
            _response(
                "end_turn",
                [
                    {
                        "type": "text",
                        "text": '{"summary":"No findings.","findings":[]}',
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(nodes, "_call_model", lambda **_: next(responses))
    fixture = FIXTURES / "sandbox-escape"

    result = review_local(
        str(fixture / "repo"),
        (fixture / "head.diff").read_text(),
        run_id="test-local",
    )

    assert result.status == "published"
    tool_calls = [
        tool
        for step in result.trace
        for tool in step.get("tools", [])
    ]
    assert tool_calls
    assert all("evals/" not in json.dumps(call["input"]).lower() for call in tool_calls)
    config.get_settings.cache_clear()
