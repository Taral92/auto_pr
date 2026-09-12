import hashlib
import json
import ssl
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any

import certifi

from core.errors import PermanentError, TransientError

API = "https://api.github.com"
USER_AGENT = "auto-pr"

_TRANSIENT_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}

#: Never trust a header to park a run for longer than this; `core.backoff`
#: clamps again, this is just to keep an absurd value out of the error object.
_MAX_HINT_S = 3600


def retry_hint(headers: Any, *, now: float | None = None) -> float | None:
    """Seconds GitHub wants us to wait, from its own headers, or None.

    Three forms, in GitHub's own order of preference:

    * `Retry-After` - seconds, or an HTTP date. Sent for secondary rate limits
      and abuse detection, and it is authoritative.
    * `X-RateLimit-Remaining: 0` plus `X-RateLimit-Reset` - the primary hourly
      limit. The reset is an epoch second, so the wait is reset - now.
    * Neither: None, and the caller falls back to exponential backoff.

    Anything unparseable is treated as absent rather than raising: a retry hint
    is an optimisation, and failing to read one must never turn a retryable
    error into a crash.
    """
    if headers is None:
        return None

    def get(name: str) -> str | None:
        try:
            return headers.get(name)
        except Exception:
            return None

    raw = get("Retry-After")
    if raw:
        try:
            return max(0.0, min(float(str(raw).strip()), _MAX_HINT_S))
        except ValueError:
            pass
        try:
            stamp = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError):
            stamp = None                 # unreadable hint is no hint
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            clock = time.time() if now is None else now
            return max(0.0, min(stamp.timestamp() - clock, _MAX_HINT_S))
    if str(get("X-RateLimit-Remaining")).strip() == "0":
        reset = get("X-RateLimit-Reset")
        try:
            clock = time.time() if now is None else now
            return max(0.0, min(float(str(reset).strip()) - clock, _MAX_HINT_S))
        except (TypeError, ValueError):
            return None
    return None


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


MARKER = "<!-- auto-pr:{key} -->"
FINDING_MARKER = "<!-- auto-pr-f:{key} -->"


def finding_key(path: str, category: str, evidence: str) -> str:
    """Stable id for one finding, independent of how the model worded it.

    Keyed on the CODE, not the prose: the same defect gets a different title
    and body on every run, so wording-based dedupe never fires.
    """
    norm = " ".join(evidence.split())
    return hashlib.sha256(f"{path}|{category}|{norm}".encode()).hexdigest()[:12]


def posted_finding_keys(owner: str, repo: str, number: int, token: str) -> set[str]:
    """Every finding key already visible on this PR, from any prior review.

    `already_reviewed` only tells you whether THIS exact review ran before.
    It cannot stop two different runs from posting the same defect twice,
    which is what happened on PR #3.
    """
    keys: set[str] = set()
    for url in (
        f"{API}/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100",
        f"{API}/repos/{owner}/{repo}/pulls/{number}/comments?per_page=100",
    ):
        try:
            body, _ = _request("GET", url, token,
                               accept="application/vnd.github+json")
        except PermanentError:
            continue          # cannot check -> post. A duplicate beats silence.
        for item in json.loads(body):
            text = item.get("body") or ""
            start = 0
            while True:
                i = text.find("<!-- auto-pr-f:", start)
                if i < 0:
                    break
                j = text.find(" -->", i)
                if j < 0:
                    break
                keys.add(text[i + 15:j])
                start = j
    return keys


def already_reviewed(owner: str, repo: str, number: int, token: str, key: str) -> bool:
    """Has this exact (pr, head_sha, prompt) already been reviewed?

    GitHub redelivers webhooks and Actions re-run, so without this a single PR
    collects the same comments several times. The marker is an HTML comment in
    the review body - invisible to readers, and it survives edits to the rest.
    """
    marker = MARKER.format(key=key)
    try:
        body, _ = _request(
            "GET",
            f"{API}/repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100",
            token,
            accept="application/vnd.github+json",
        )
    except PermanentError:
        return False          # can't check -> post. A duplicate beats silence.
    return any(marker in (r.get("body") or "") for r in json.loads(body))


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
    except PermanentError as e:
        if e.code != 422:
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
        ctx = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            return resp.read().decode("utf-8"), resp.status
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        msg = f"GitHub {e.code} {method} {url}: {err_body[:800]}"
        if e.code in _TRANSIENT_CODES:
            # Carry GitHub's own reset time so the worker waits that long
            # instead of guessing - and instead of re-queueing immediately,
            # which is how three attempts used to burn in six seconds.
            raise TransientError(
                msg, code=e.code, retry_after=retry_hint(e.headers)
            ) from None
        # A 403 carrying rate-limit headers is a limit, not an authorisation
        # failure. GitHub uses 403 for secondary limits, and dead-lettering
        # those would drop reviews for a condition that clears on its own.
        hint = retry_hint(e.headers) if e.code == 403 else None
        if hint is not None:
            raise TransientError(msg, code=e.code, retry_after=hint) from None
        raise PermanentError(msg, code=e.code) from None
    except urllib.error.URLError as e:
        raise TransientError(f"GitHub {method} {url} failed: {e.reason}") from None
