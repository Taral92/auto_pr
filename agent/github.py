import json
import urllib.error
import urllib.request
from typing import Any

API = "https://api.github.com"
USER_AGENT = "auto-pr"


def get_pr(owner: str, repo: str, number: int, token: str) -> dict[str, Any]:
    body, _ = _request(
        "GET",
        f"{API}/repos/{owner}/{repo}/pulls/{number}",
        token,
        accept="application/vnd.github+json",
    )
    return json.loads(body)


def get_diff(owner: str, repo: str, number: int, token: str) -> str:
    body, _ = _request(
        "GET",
        f"{API}/repos/{owner}/{repo}/pulls/{number}",
        token,
        accept="application/vnd.github.v3.diff",
    )
    return body


def post_review(
    owner: str, repo: str, number: int, token: str, payload: dict[str, Any]
) -> dict[str, Any]:
    url = f"{API}/repos/{owner}/{repo}/pulls/{number}/reviews"
    try:
        body, _ = _request(
            "POST",
            url,
            token,
            accept="application/vnd.github+json",
            data=payload,
        )
        return json.loads(body)
    except RuntimeError as e:
        if " 422 " not in str(e):
            raise
        comments = list(payload.get("comments") or [])
        if not comments:
            raise
        moved = "\n\n".join(_format_moved(c) for c in comments)
        body_text = (payload.get("body") or "").rstrip()
        retry = {
            **payload,
            "body": f"{body_text}\n\n{moved}".strip() if body_text else moved,
            "comments": [],
        }
        print(f"review POST 422; moving {len(comments)} comment(s) to body and retrying")
        body, _ = _request(
            "POST",
            url,
            token,
            accept="application/vnd.github+json",
            data=retry,
        )
        return json.loads(body)


def _format_moved(comment: dict[str, Any]) -> str:
    path = comment.get("path", "")
    line = comment.get("line")
    loc = f"{path}:{line}" if line is not None else path
    return f"**{loc}**\n\n{comment.get('body', '')}"


def _request(
    method: str,
    url: str,
    token: str,
    *,
    accept: str,
    data: dict[str, Any] | None = None,
) -> tuple[str, int]:
    raw = None if data is None else json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", USER_AGENT)
    if raw is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode("utf-8"), resp.status
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub {e.code} {method} {url}: {err_body[:800]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub {method} {url} failed: {e.reason}") from None
