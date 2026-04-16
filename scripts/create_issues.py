from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    ROOT,
    build_issue_body,
    default_repo_config,
    env,
    github_request,
    json_dump,
    json_load,
    print_json,
    slugify,
    utc_now,
    write_github_output,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create GitHub work items from normalized scanner findings.")
    parser.add_argument("--input", default=str(ROOT / "state" / "findings.json"))
    parser.add_argument("--output", default=str(ROOT / "state" / "issues.json"))
    return parser.parse_args()


def existing_issues(owner: str, repo: str, token: str) -> dict[str, dict]:
    issues = github_request(
        "GET",
        f"/repos/{owner}/{repo}/issues",
        token=token,
        query={"state": "open", "labels": "security-remediation"},
    )
    result = {}
    for issue in issues:
        title = issue.get("title", "")
        result[title] = issue
    return result


def title_for_finding(finding: dict) -> str:
    return (
        f"Remediate {finding['severity']} vulnerability in "
        f"{finding['package']} ({finding['ecosystem']})"
    )


def ensure_labels(owner: str, repo: str, token: str, labels: list[str]) -> None:
    existing = github_request("GET", f"/repos/{owner}/{repo}/labels", token=token, query={"per_page": "100"})
    existing_names = {item["name"] for item in existing}
    palette = {
        "security-remediation": ("d73a4a", "Scanner-derived security work item"),
        "devin-candidate": ("5319e7", "Eligible for Devin automation"),
        "devin-remediate": ("0e8a16", "Trigger Devin remediation from this finding"),
    }
    for label in labels:
        if label in existing_names:
            continue
        color, description = palette.get(label, ("1d76db", "Automation-managed label"))
        github_request(
            "POST",
            f"/repos/{owner}/{repo}/labels",
            token=token,
            payload={"name": label, "color": color, "description": description},
        )


def create_issue(owner: str, repo: str, token: str, finding: dict) -> dict:
    title = title_for_finding(finding)
    labels = finding["labels"] + ["devin-remediate", f"finding:{slugify(finding['id'])}"]
    ensure_labels(owner, repo, token, labels)
    payload = {
        "title": title,
        "body": build_issue_body(finding),
        "labels": labels,
    }
    issue = github_request("POST", f"/repos/{owner}/{repo}/issues", token=token, payload=payload)
    return issue


def main() -> None:
    args = parse_args()
    token = env("GH_TOKEN")
    owner, repo = default_repo_config()
    findings = json_load(Path(args.input), default={}).get("findings", [])
    current_issues = existing_issues(owner, repo, token)

    records = []
    created_count = 0
    for finding in findings:
        title = title_for_finding(finding)
        issue = current_issues.get(title)
        created = False
        if issue is None:
            issue = create_issue(owner, repo, token, finding)
            created = True
            created_count += 1
        records.append(
            {
                "finding_id": finding["id"],
                "issue_number": issue["number"],
                "issue_url": issue["html_url"],
                "issue_title": issue["title"],
                "created": created,
                "synced_at": utc_now(),
            }
        )

    payload = {"generated_at": utc_now(), "count": len(records), "created_count": created_count, "issues": records}
    json_dump(Path(args.output), payload)
    write_github_output("issues_created", str(created_count))
    print_json(payload)


if __name__ == "__main__":
    main()
