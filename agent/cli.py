import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from .review import review_pr

PR_URL_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = PR_URL_RE.search(url)
    if not m:
        raise SystemExit(f"unrecognized PR URL: {url}")
    repo = m.group("repo").removesuffix(".git")
    return m.group("owner"), repo, int(m.group("number"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agent.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    review = sub.add_parser("review", help="Review a GitHub pull request")
    review.add_argument("pr_url")
    review.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the review payload; do not POST",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is not set")

    owner, repo, number = parse_pr_url(args.pr_url)
    result = review_pr(owner, repo, number, token, dry_run=args.dry_run)
    print(json.dumps(result.payload, indent=2))
    print(
        f"grounding={result.grounding} anchoring={result.anchoring} "
        f"posted={result.posted} temp_dir_removed={result.temp_dir_removed}"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
