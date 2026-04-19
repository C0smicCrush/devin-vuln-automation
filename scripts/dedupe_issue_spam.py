"""Delete consecutive duplicate comments on issues in the target repo.

Usage:
    python dedupe_issue_spam.py --dry-run
    python dedupe_issue_spam.py            # actually deletes

Relies on `gh` CLI being authenticated with delete permissions on the repo.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any


REPO = "C0smicCrush/superset-remediation"

HEADING_RE = re.compile(
    r"^AWS (?:poller|verification|remediation worker)[^\n]*\n+",
)


def gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh"] + args,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def list_issue_numbers() -> list[int]:
    data = gh_json(
        [
            "issue",
            "list",
            "-R",
            REPO,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number",
        ]
    )
    return [int(row["number"]) for row in data]


def list_comments(issue: int) -> list[dict]:
    return gh_json(
        [
            "api",
            f"repos/{REPO}/issues/{issue}/comments",
            "--paginate",
        ]
    )


def delete_comment(comment_id: int) -> None:
    subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "DELETE",
            f"repos/{REPO}/issues/comments/{comment_id}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def normalize_body(body: str, *, strip_heading: bool) -> str:
    """Normalize a comment body for dup comparison.

    When strip_heading is True, drop the first "AWS ... update." style heading
    line so that a poller update and a verification update reporting the
    same session/status/detail/summary are considered duplicates.
    """
    text = (body or "").strip()
    if strip_heading:
        text = HEADING_RE.sub("", text, count=1)
    return text.strip()


def find_consecutive_duplicates(
    comments: list[dict], *, strip_heading: bool
) -> list[dict]:
    """Return comments to delete: any comment whose normalized body equals the
    previous comment's normalized body (same author) is a consecutive dup."""
    to_delete: list[dict] = []
    prev_norm: str | None = None
    prev_user: str | None = None
    for c in comments:
        norm = normalize_body(c.get("body") or "", strip_heading=strip_heading)
        user = (c.get("user") or {}).get("login")
        if (
            prev_norm is not None
            and norm == prev_norm
            and user == prev_user
            and norm != ""
        ):
            to_delete.append(c)
        else:
            prev_norm = norm
            prev_user = user
    return to_delete


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Only process a single issue number.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Only delete comments whose full body exactly equals the "
            "previous comment. Default is to also normalize the leading "
            "'AWS poller/verification ... update.' heading before comparing."
        ),
    )
    args = parser.parse_args()
    strip_heading = not args.strict

    issue_numbers = [args.issue] if args.issue else list_issue_numbers()
    print(f"Scanning {len(issue_numbers)} issue(s) in {REPO}")

    total_found = 0
    total_deleted = 0

    for num in issue_numbers:
        comments = list_comments(num)
        dups = find_consecutive_duplicates(
            comments, strip_heading=strip_heading
        )
        if not dups:
            print(f"  #{num}: {len(comments)} comments, 0 consecutive dupes")
            continue
        total_found += len(dups)
        print(
            f"  #{num}: {len(comments)} comments, "
            f"{len(dups)} consecutive dupes to delete"
        )
        for c in dups:
            preview = (c.get("body") or "").splitlines()[0][:90]
            if args.dry_run:
                print(f"    DRY-RUN would delete {c['id']} :: {preview}")
            else:
                try:
                    delete_comment(int(c["id"]))
                    total_deleted += 1
                    print(f"    deleted {c['id']} :: {preview}")
                except subprocess.CalledProcessError as exc:
                    print(
                        f"    FAILED to delete {c['id']}: "
                        f"{exc.stderr.strip()}",
                        file=sys.stderr,
                    )

    print(
        f"\nDone. duplicates_found={total_found} "
        f"deleted={total_deleted} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
