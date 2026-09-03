import re

from .models import Finding, Verdict

MIN_EVIDENCE = 20


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _long_enough(evidence: str) -> bool:
    stripped = evidence.strip("\n")
    return "\n" in stripped or len(evidence.strip()) >= MIN_EVIDENCE


def _post_image_corpus(diff: str) -> list[tuple[str, str]]:
    """Reconstruct the visible post-image lines from each file's diff hunks."""
    corpus: list[tuple[str, str]] = []
    path: str | None = None
    lines: list[str] = []
    in_hunk = False

    def flush() -> None:
        if path is not None and lines:
            corpus.append((f"diff-post:{path}", "\n".join(lines)))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            path = None
            lines = []
            in_hunk = False
        elif line.startswith("+++ "):
            raw = line[4:].strip()
            path = raw[2:] if raw.startswith("b/") else raw
        elif line.startswith("@@"):
            if in_hunk and lines:
                lines.append("")
            in_hunk = True
        elif in_hunk and line.startswith(("+", " ")):
            lines.append(line[1:])
        elif in_hunk and line.startswith("-"):
            continue
    flush()
    return corpus


def ground(
    findings: list[Finding],
    diff: str,
    tool_results: list[tuple[str, str]],
) -> list[tuple[Finding, Verdict, str | None]]:
    """Return (finding, verdict, source) for each finding.

    Corpus is the diff plus every tool_result. Keep grounded; caller drops the rest.
    """
    corpus: list[tuple[str, str]] = [
        ("diff", diff),
        *_post_image_corpus(diff),
        *tool_results,
    ]
    out: list[tuple[Finding, Verdict, str | None]] = []
    for finding in findings:
        evidence = finding.evidence
        if not _long_enough(evidence):
            out.append((finding, "ungrounded", None))
            continue
        verdict, source = _verdict(evidence, corpus)
        out.append((finding, verdict, source))
    return out


def _verdict(evidence: str, corpus: list[tuple[str, str]]) -> tuple[Verdict, str | None]:
    for source, text in corpus:
        if evidence in text:
            return "grounded", source
    near = _norm(evidence)
    if not near:
        return "ungrounded", None
    for source, text in corpus:
        if near in _norm(text):
            return "near", source
    return "ungrounded", None


def counts(rows: list[tuple[Finding, Verdict, str | None]]) -> dict[str, int]:
    tally = {"grounded": 0, "near": 0, "ungrounded": 0}
    for _, verdict, _ in rows:
        tally[verdict] += 1
    return tally






