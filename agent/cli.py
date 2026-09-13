import argparse
import json
import sys

from pydantic import ValidationError

from config import get_settings
from gh.urls import parse_pr_url

from .review import review_pr


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

    try:
        token = get_settings().github_token.get_secret_value()
    except ValidationError:
        raise SystemExit("GITHUB_TOKEN is not set") from None

    try:
        owner, repo, number = parse_pr_url(args.pr_url)
    except ValueError as e:
        raise SystemExit(str(e)) from None

    # write_trace=True: on the CLI the runs/ file is the only record there
    # is. The worker leaves it to the setting (off) because Postgres has it.
    result = review_pr(owner, repo, number, token, dry_run=args.dry_run,
                       write_trace=True)
    print(json.dumps(result.payload, indent=2))
    print(
        f"grounding={result.grounding} anchoring={result.anchoring} "
        f"posted={result.posted} temp_dir_removed={result.temp_dir_removed}"
    )


if __name__ == "__main__":
    main(sys.argv[1:])
