from core.diff import post_images
from core.models import Finding

TOO_LARGE = "Diff too large to review."


def parse_anchorable(diff: str) -> dict[str, set[int]]:
    """Map each path to the set of RIGHT-side line numbers GitHub will accept."""
    anchorable: dict[str, set[int]] = {}
    for image in post_images(diff):
        anchorable.setdefault(image.path, set()).update(
            line for line in image.line_numbers if line is not None
        )
    return anchorable


def locate_in_diff(diff: str, evidence: str) -> tuple[str, int] | None:
    """Return the first post-image line spanned by `evidence`."""
    for image in post_images(diff):
        line = image.locate(evidence)
        if line is not None:
            return image.path, line
    return None


def finding_body(finding: Finding) -> str:
    parts = [finding.title, "", finding.description]
    if finding.recommendation:
        parts.extend(["", f"Recommendation: {finding.recommendation}"])
    return "\n".join(parts).strip()


def too_large_payload(head_sha: str) -> dict:
    return {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": TOO_LARGE,
        "comments": [],
    }


def build_review(
    items: list[dict],
    diff: str,
    head_sha: str,
    summary: str,
) -> tuple[list[dict], dict, dict]:
    anchorable = parse_anchorable(diff)
    published: list[dict] = []
    comments: list[dict] = []
    summary_items: list[str] = []
    for item in items:
        finding = Finding.model_validate(
            {k: item[k] for k in Finding.model_fields if k in item}
        )
        body = finding_body(finding)
        verdict = item.get("verdict") or "ungrounded"
        if verdict != "grounded":
            published.append(
                {
                    **item,
                    "path": finding.file,
                    "line": None,
                    "body": body,
                    "anchored": "dropped",
                    "posted": False,
                }
            )
            continue
        loc = locate_in_diff(diff, finding.evidence)
        if loc is not None and loc[1] in anchorable.get(loc[0], set()):
            path, line = loc
            comments.append(
                {"path": path, "line": line, "side": "RIGHT", "body": body}
            )
            published.append(
                {
                    **item,
                    "path": path,
                    "line": line,
                    "body": body,
                    "anchored": "inline",
                    "posted": False,
                }
            )
        else:
            path = loc[0] if loc is not None else finding.file
            line = loc[1] if loc is not None else None
            loc_label = f"{path}:{line}" if line is not None else path
            summary_items.append(f"**{loc_label}** — {body}")
            published.append(
                {
                    **item,
                    "path": path,
                    "line": line,
                    "body": body,
                    "anchored": "summary",
                    "posted": False,
                }
            )
    tally = {"inline": 0, "summary": 0, "dropped": 0}
    for f in published:
        key = f.get("anchored")
        if key in tally:
            tally[key] += 1
    # The model's summary asserts the findings. If none survived grounding, the
    # summary is unverified too and must not be published.
    surviving = tally["inline"] + tally["summary"]
    body_parts: list[str] = []
    if surviving and summary.strip():
        body_parts.append(summary.strip())
    body_parts.extend(summary_items)
    if not body_parts:
        if tally["dropped"]:
            body_parts.append(
                f"Reviewed. {tally['dropped']} finding(s) were discarded because "
                "their evidence could not be matched against the diff or the "
                "files read."
            )
        else:
            body_parts.append("Reviewed, nothing found.")
    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": "\n\n".join(body_parts).strip(),
        "comments": comments,
    }
    return published, payload, tally
