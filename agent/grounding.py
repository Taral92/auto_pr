import re

from core.diff import post_images

from .models import Finding, Verdict

MIN_EVIDENCE = 20


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _long_enough(evidence: str) -> bool:
    stripped = evidence.strip("\n")
    return "\n" in stripped or len(evidence.strip()) >= MIN_EVIDENCE


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
        *((f"diff-post:{image.path}", image.text) for image in post_images(diff)),
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






