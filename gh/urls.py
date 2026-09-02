import re

PR_URL_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.search(url)
    if not m:
        raise ValueError(f"unrecognized PR URL: {url}")
    repo = m.group("repo").removesuffix(".git")
    return m.group("owner"), repo, int(m.group("number"))
