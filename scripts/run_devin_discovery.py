from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    ROOT,
    build_discovery_prompt,
    canonical_issue_body_from_work_item,
    default_repo_config,
    devin_request,
    discovery_output_schema,
    env,
    github_request,
    json_dump,
    json_load,
    print_json,
    slugify,
    utc_now,
)

ACTIVE_STATUSES = {"new", "creating", "claimed", "running", "resuming", "waiting_for_user"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a bounded Devin discovery pass and create tracked issues.")
    parser.add_argument("--max-findings", type=int, default=1)
    parser.add_argument("--state-file", default=str(ROOT / "state" / "discovery.json"))
    parser.add_argument("--poll-timeout-seconds", type=int, default=900)
    return parser.parse_args()


def poll_session_until_terminal(devin_org_id: str, devin_api_key: str, session_id: str, timeout_seconds: int) -> dict:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        session = devin_request(
            "GET",
            f"/v3/organizations/{devin_org_id}/sessions/{session_id}",
            api_key=devin_api_key,
        )
        if session["status"] in {"exit", "error", "suspended"}:
            return session
        if session["status"] == "waiting_for_user" and session.get("structured_output"):
            return session
        time.sleep(10)
    raise SystemExit(f"Timed out waiting for discovery session {session_id}")


def list_project_sessions(devin_org_id: str, devin_api_key: str, phase: str) -> list[dict]:
    payload = devin_request(
        "GET",
        f"/v3/organizations/{devin_org_id}/sessions?tags=project%3Adevin-vuln-automation&tags=phase%3A{phase}&first=100",
        api_key=devin_api_key,
    )
    return payload.get("items") or payload.get("sessions") or []


def has_active_discovery_session(devin_org_id: str, devin_api_key: str) -> bool:
    for session in list_project_sessions(devin_org_id, devin_api_key, "discovery"):
        if session.get("status") in ACTIVE_STATUSES:
            return True
    return False


def ensure_labels(owner: str, repo: str, token: str, labels: list[str]) -> None:
    existing = github_request("GET", f"/repos/{owner}/{repo}/labels", token=token, query={"per_page": "100"})
    existing_names = {item["name"] for item in existing}
    palette = {
        "security-remediation": ("d73a4a", "Security remediation work item"),
        "devin-remediate": ("0e8a16", "Trigger Devin remediation from this finding"),
        "devin-discovered": ("5319e7", "Created from a bounded Devin discovery run"),
        "manual-source": ("5319e7", "Submitted through the manual intake endpoint"),
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


def existing_open_issues(owner: str, repo: str, token: str) -> list[dict]:
    return github_request(
        "GET",
        f"/repos/{owner}/{repo}/issues",
        token=token,
        query={"state": "open", "per_page": "100"},
    )


def should_create_issue(existing_issues: list[dict], finding: dict) -> bool:
    desired_finding_label = f"finding:{slugify(finding['id'])}"
    for issue in existing_issues:
        issue_labels = {item["name"] for item in issue.get("labels", [])}
        if issue.get("title") == finding["title"]:
            return False
        if desired_finding_label in issue_labels:
            return False
    return True


def create_issue_from_finding(owner: str, repo: str, token: str, finding: dict, session_url: str) -> dict:
    work_item = {
        "problem_statement": finding["problem_statement"],
        "scope_tier": finding["scope_tier"],
        "automation_decision": finding["automation_decision"],
        "confidence": finding["confidence"],
        "source": {
            "type": "devin_discovery",
            "id": finding["id"],
            "url": session_url,
            "action": "discovered",
        },
        "test_plan": finding["test_plan"],
    }
    body = canonical_issue_body_from_work_item(work_item)
    body += (
        "\n\n## Discovery Evidence\n"
        f"{finding['evidence']}\n"
        "\n## Discovery Notes\n"
        f"- Created from bounded Devin discovery session: {session_url}\n"
        "- This issue should be re-validated by the remediation session before code changes are made.\n"
        "- Remediation PR must include before/after scanner receipts and the exact test commands run.\n"
        "- Keep the PR bounded to this advisory; split any additional CVEs into separate tracked issues.\n"
    )
    labels = list(dict.fromkeys(finding["issue_labels"] + ["devin-remediate", "devin-discovered", f"finding:{slugify(finding['id'])}"]))
    ensure_labels(owner, repo, token, labels)
    return github_request(
        "POST",
        f"/repos/{owner}/{repo}/issues",
        token=token,
        payload={"title": finding["title"], "body": body, "labels": labels},
    )


def main() -> None:
    args = parse_args()
    gh_token = env("GH_TOKEN")
    devin_api_key = env("DEVIN_API_KEY")
    devin_org_id = env("DEVIN_ORG_ID")
    owner, repo = default_repo_config()
    repo_url = f"https://github.com/{owner}/{repo}"

    if has_active_discovery_session(devin_org_id, devin_api_key):
        raise SystemExit("A Devin discovery session is already active; refusing to launch another one.")

    payload = {
        "title": f"Discover remediation candidates in {repo}",
        "prompt": build_discovery_prompt(owner, repo, repo_url, args.max_findings),
        "advanced_mode": "analyze",
        "repos": [repo_url],
        "max_acu_limit": 1,
        "structured_output_schema": discovery_output_schema(),
        "tags": [
            "project:devin-vuln-automation",
            "phase:discovery",
            f"repo:{slugify(repo)}",
        ],
    }
    session = devin_request(
        "POST",
        f"/v3/organizations/{devin_org_id}/sessions",
        api_key=devin_api_key,
        payload=payload,
    )
    final_session = poll_session_until_terminal(devin_org_id, devin_api_key, session["session_id"], args.poll_timeout_seconds)
    structured = final_session.get("structured_output") or {"summary": "", "findings": []}
    findings = structured.get("findings", [])
    rejected = structured.get("rejected_findings") or []
    existing_issues = existing_open_issues(owner, repo, gh_token)

    created = []
    skipped = []
    for finding in findings[: args.max_findings]:
        if finding.get("automation_decision") not in {"auto", "manual_approval", "auto-create-issue"}:
            skipped.append({"id": finding["id"], "reason": "unsupported_automation_decision"})
            continue
        if str(finding.get("confidence", "")).lower() not in {"high", "medium"}:
            skipped.append({"id": finding["id"], "reason": "low_confidence"})
            continue
        if not should_create_issue(existing_issues, finding):
            skipped.append({"id": finding["id"], "reason": "duplicate_open_issue"})
            continue
        issue = create_issue_from_finding(owner, repo, gh_token, finding, session["url"])
        created.append(
            {
                "finding_id": finding["id"],
                "issue_number": issue["number"],
                "issue_url": issue["html_url"],
                "issue_title": issue["title"],
            }
        )
        existing_issues.append(issue)

    output = {
        "generated_at": utc_now(),
        "session_id": session["session_id"],
        "session_url": session["url"],
        "status": final_session["status"],
        "summary": structured.get("summary", ""),
        "findings_count": len(findings),
        "issues_created": len(created),
        "created": created,
        "skipped": skipped,
        "rejected_findings": rejected,
    }
    json_dump(Path(args.state_file), output)
    print_json(output)


if __name__ == "__main__":
    main()
