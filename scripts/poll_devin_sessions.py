from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    ROOT,
    default_repo_config,
    devin_request,
    env,
    github_request,
    json_dump,
    json_load,
    print_json,
    utc_now,
    write_github_output,
)


TERMINAL_STATUSES = {"exit", "error", "suspended"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Poll active Devin sessions and mirror state back to GitHub.")
    parser.add_argument("--state-file", default=str(ROOT / "state" / "sessions.json"))
    parser.add_argument("--metrics-file", default=str(ROOT / "metrics" / "latest.json"))
    return parser.parse_args()


def build_comment(session: dict, previous: dict | None) -> str | None:
    old_status = previous.get("status") if previous else None
    new_status = session["status"]
    prs = session.get("pull_requests", [])
    first_pr = prs[0]["pr_url"] if prs else None
    old_prs = previous.get("pull_requests", []) if previous else []
    old_first_pr = old_prs[0]["pr_url"] if old_prs else None

    if new_status == old_status and first_pr == old_first_pr:
        return None

    lines = [
        "Devin session status update.",
        "",
        f"- Session ID: `{session['session_id']}`",
        f"- Status: `{new_status}`",
    ]
    if session.get("status_detail"):
        lines.append(f"- Detail: `{session['status_detail']}`")
    if first_pr:
        lines.append(f"- Pull request: {first_pr}")
    if session.get("structured_output"):
        summary = session["structured_output"].get("summary")
        result = session["structured_output"].get("result")
        if result:
            lines.append(f"- Result: `{result}`")
        if summary:
            lines.append(f"- Summary: {summary}")
    return "\n".join(lines)


def parse_issue_number(tags: list[str]) -> int | None:
    for tag in tags:
        if tag.startswith("issue:"):
            try:
                return int(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


def list_tagged_sessions(devin_org_id: str, devin_api_key: str) -> list[dict]:
    payload = devin_request(
        "GET",
        f"/v3/organizations/{devin_org_id}/sessions?tags=project%3Adevin-vuln-automation&first=100",
        api_key=devin_api_key,
    )
    sessions = payload.get("items") or payload.get("sessions") or payload
    results = []
    for session in sessions:
        issue_number = parse_issue_number(session.get("tags", []))
        if not issue_number:
            continue
        results.append(
            {
                "issue_number": issue_number,
                "session_id": session["session_id"],
                "status": session["status"],
                "status_detail": session.get("status_detail"),
                "pull_requests": session.get("pull_requests", []),
                "structured_output": session.get("structured_output"),
                "session_url": session["url"],
                "updated_at": session.get("updated_at"),
            }
        )
    return results


def main() -> None:
    args = parse_args()
    gh_token = env("GH_TOKEN")
    devin_api_key = env("DEVIN_API_KEY")
    devin_org_id = env("DEVIN_ORG_ID")
    owner, repo = default_repo_config()

    state_path = Path(args.state_file)
    current = json_load(state_path, default={"sessions": []})
    current_sessions = current.get("sessions", [])
    if not current_sessions:
        current_sessions = list_tagged_sessions(devin_org_id, devin_api_key)
    updated_records = []
    metrics = {
        "generated_at": utc_now(),
        "total_sessions": 0,
        "active_sessions": 0,
        "completed_sessions": 0,
        "blocked_sessions": 0,
        "failed_sessions": 0,
        "pull_requests_opened": 0,
        "sessions": [],
    }

    for record in current_sessions:
        latest = devin_request(
            "GET",
            f"/v3/organizations/{devin_org_id}/sessions/{record['session_id']}",
            api_key=devin_api_key,
        )
        comment = build_comment(latest, record)
        if comment:
            github_request(
                "POST",
                f"/repos/{owner}/{repo}/issues/{record['issue_number']}/comments",
                token=gh_token,
                payload={"body": comment},
            )

        merged = {
            **record,
            "status": latest["status"],
            "status_detail": latest.get("status_detail"),
            "pull_requests": latest.get("pull_requests", []),
            "structured_output": latest.get("structured_output"),
            "updated_at": latest["updated_at"],
            "session_url": latest["url"],
        }
        updated_records.append(merged)
        metrics["total_sessions"] += 1
        if latest["status"] in {"new", "creating", "claimed", "running", "resuming"}:
            metrics["active_sessions"] += 1
        elif latest["status"] == "exit":
            metrics["completed_sessions"] += 1
        elif latest["status"] == "suspended":
            metrics["blocked_sessions"] += 1
        elif latest["status"] == "error":
            metrics["failed_sessions"] += 1
        if latest.get("pull_requests"):
            metrics["pull_requests_opened"] += len(latest["pull_requests"])
        metrics["sessions"].append(
            {
                "issue_number": record["issue_number"],
                "session_id": record["session_id"],
                "status": latest["status"],
                "status_detail": latest.get("status_detail"),
                "pull_requests": latest.get("pull_requests", []),
            }
        )

    json_dump(state_path, {"generated_at": utc_now(), "sessions": updated_records})
    json_dump(Path(args.metrics_file), metrics)
    write_github_output("active_sessions", str(metrics["active_sessions"]))
    write_github_output("completed_sessions", str(metrics["completed_sessions"]))
    print_json(metrics)


if __name__ == "__main__":
    main()
