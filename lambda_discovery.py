from __future__ import annotations

from scripts.common import build_discovery_prompt, devin_request, discovery_output_schema, slugify, utc_now
from scripts.run_devin_discovery import (
    create_issue_from_finding,
    existing_open_issues,
    has_active_discovery_session as local_has_active_discovery_session,
    poll_session_until_terminal,
    should_create_issue,
)

from aws_runtime import acquire_discovery_lock, has_active_discovery_session, load_runtime_settings, release_discovery_lock


def _launch_discovery_session(settings: dict, max_findings: int) -> dict:
    payload = {
        "title": f"Discover remediation candidates in {settings['repo']}",
        "prompt": build_discovery_prompt(settings["owner"], settings["repo"], settings["repo_url"], max_findings),
        "advanced_mode": "analyze",
        "repos": [settings["repo_url"]],
        "max_acu_limit": 1,
        "structured_output_schema": discovery_output_schema(),
        "tags": [
            "project:devin-vuln-automation",
            "phase:discovery",
            f"repo:{slugify(settings['repo'])}",
        ],
    }
    return devin_request(
        "POST",
        f"/v3/organizations/{settings['devin_org_id']}/sessions",
        api_key=settings["devin_api_key"],
        payload=payload,
    )


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    max_findings = int((event or {}).get("max_findings") or settings["max_discovery_findings"])
    holder = getattr(context, "aws_request_id", f"discovery-{utc_now()}")

    if not acquire_discovery_lock(settings, holder, settings["discovery_lock_ttl_seconds"]):
        return {"action": "lock_skipped", "reason": "discovery_lock_held"}

    try:
        if has_active_discovery_session(settings) or local_has_active_discovery_session(
            settings["devin_org_id"], settings["devin_api_key"]
        ):
            return {"action": "active_session_skipped", "reason": "existing_discovery_session"}

        session = _launch_discovery_session(settings, max_findings)
        final_session = poll_session_until_terminal(
            settings["devin_org_id"],
            settings["devin_api_key"],
            session["session_id"],
            settings["discovery_timeout_seconds"],
        )
        structured = final_session.get("structured_output") or {"summary": "", "findings": []}
        findings = structured.get("findings", [])
        open_issues = existing_open_issues(settings["owner"], settings["repo"], settings["gh_token"])

        created = []
        skipped = []
        for finding in findings[:max_findings]:
            if finding.get("automation_decision") not in {"auto", "manual_approval", "auto-create-issue"}:
                skipped.append({"id": finding["id"], "reason": "unsupported_automation_decision"})
                continue
            if str(finding.get("confidence", "")).lower() not in {"high", "medium"}:
                skipped.append({"id": finding["id"], "reason": "low_confidence"})
                continue
            if not should_create_issue(open_issues, finding):
                skipped.append({"id": finding["id"], "reason": "duplicate_open_issue"})
                continue
            issue = create_issue_from_finding(
                settings["owner"],
                settings["repo"],
                settings["gh_token"],
                finding,
                session["url"],
            )
            created.append(
                {
                    "finding_id": finding["id"],
                    "issue_number": issue["number"],
                    "issue_url": issue["html_url"],
                    "issue_title": issue["title"],
                }
            )
            open_issues.append(issue)

        return {
            "action": "completed",
            "generated_at": utc_now(),
            "session_id": session["session_id"],
            "session_url": session["url"],
            "status": final_session["status"],
            "summary": structured.get("summary", ""),
            "findings_count": len(findings),
            "issues_created": len(created),
            "created": created,
            "skipped": skipped,
        }
    finally:
        release_discovery_lock(settings)
