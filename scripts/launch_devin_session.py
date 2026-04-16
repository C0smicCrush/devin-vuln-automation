from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    ROOT,
    build_devin_prompt,
    default_repo_config,
    devin_request,
    env,
    github_request,
    json_dump,
    json_load,
    print_json,
    session_output_schema,
    slugify,
    utc_now,
    write_github_output,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a Devin remediation session for a GitHub issue.")
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--state-file", default=str(ROOT / "state" / "sessions.json"))
    return parser.parse_args()


def to_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def main() -> None:
    args = parse_args()
    gh_token = env("GH_TOKEN")
    devin_api_key = env("DEVIN_API_KEY")
    devin_org_id = env("DEVIN_ORG_ID")
    owner, repo = default_repo_config()
    repo_url = f"https://github.com/{owner}/{repo}"

    issue = github_request("GET", f"/repos/{owner}/{repo}/issues/{args.issue_number}", token=gh_token)
    session_title = f"Remediate #{issue['number']} {issue['title'][:80]}"
    prompt = build_devin_prompt(owner, repo, issue, repo_url)
    bypass_approval = to_bool(env("DEVIN_BYPASS_APPROVAL", "false"))

    payload = {
        "title": session_title,
        "prompt": prompt,
        "advanced_mode": "create",
        "repos": [repo_url],
        "bypass_approval": bypass_approval,
        "tags": [
            "project:devin-vuln-automation",
            f"repo:{slugify(repo)}",
            f"issue:{issue['number']}",
        ],
        "structured_output_schema": session_output_schema(),
    }
    session = devin_request(
        "POST",
        f"/v3/organizations/{devin_org_id}/sessions",
        api_key=devin_api_key,
        payload=payload,
    )

    state_path = Path(args.state_file)
    existing = json_load(state_path, default={"sessions": []})
    existing["sessions"] = [item for item in existing.get("sessions", []) if item.get("issue_number") != issue["number"]]
    existing["sessions"].append(
        {
            "issue_number": issue["number"],
            "issue_url": issue["html_url"],
            "issue_title": issue["title"],
            "session_id": session["session_id"],
            "session_url": session["url"],
            "status": session["status"],
            "pull_requests": session.get("pull_requests", []),
            "launched_at": utc_now(),
        }
    )
    existing["generated_at"] = utc_now()
    json_dump(state_path, existing)

    comment_body = (
        "Devin remediation session launched.\n\n"
        f"- Session ID: `{session['session_id']}`\n"
        f"- Session URL: {session['url']}\n"
        f"- Initial status: `{session['status']}`"
    )
    github_request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue['number']}/comments",
        token=gh_token,
        payload={"body": comment_body},
    )

    write_github_output("session_id", session["session_id"])
    write_github_output("session_url", session["url"])
    print_json(session)


if __name__ == "__main__":
    main()
