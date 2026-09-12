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
                        "id": "read-1",
                        "name": "read_file",
                        "input": {"path": "app/files.py"},
                    }
                ],
            ),
            # Completion is a tool call now, not a JSON blob in a text block.
            _response(
                "tool_use",
                [
                    {
                        "type": "tool_use",
                        "id": "submit-1",
                        "name": "submit_findings",
                        "input": {"summary": "No findings.", "findings": []},
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


def test_an_exhausted_run_is_forced_to_submit_and_marked(monkeypatch):
    """The whole degraded path, through the real graph.

    Previously this path ran the final turn's tools and threw the results
    away unread, then reached for findings in a text block that was never
    findings, then paid for an untracked repair call to sort it out. Now the
    breach diverts before dispatch and one constrained, counted call ends it.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("GITHUB_TOKEN", "")
    config.get_settings.cache_clear()
    cap = config.get_settings().max_tokens_total

    calls: list[dict] = []

    def fake_call(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # One turn that blows the token budget and asks for a file.
            return _Response(
                {
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": cap, "output_tokens": 1},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "read-1",
                            "name": "read_file",
                            "input": {"path": "app/files.py"},
                        }
                    ],
                }
            )
        return _Response(
            {
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 2},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "submit-1",
                        "name": "submit_findings",
                        "input": {"summary": "Cut short.", "findings": []},
                    }
                ],
            }
        )

    monkeypatch.setattr(nodes, "_call_model", fake_call)
    fixture = FIXTURES / "sandbox-escape"

    result = review_local(
        str(fixture / "repo"),
        (fixture / "head.diff").read_text(),
        run_id="test-exhausted",
    )

    assert result.status == "degraded"
    # The breach reason survives to the end. It used to be overwritten by
    # whatever error the parser raised on its way out.
    assert result.error == "budget_breach:tokens"
    # The forced call is constrained, and counted: cap + 5 in, not cap.
    assert calls[-1]["tool_choice"] == {"type": "tool", "name": "submit_findings"}
    assert result.tokens_in == cap + 5
    assert result.tokens_out == 3
    # The breached turn's read was never dispatched - nothing but the diff.
    assert [c["source"] for c in result.corpus] == ["diff"]
    assert result.trace[-1]["forced_submit"] == "tokens"
    config.get_settings.cache_clear()
