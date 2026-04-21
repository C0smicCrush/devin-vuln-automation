from __future__ import annotations

from scripts.common import build_discovery_prompt, devin_request, discovery_output_schema, slugify, utc_now
from scripts.run_devin_discovery import (
    has_active_discovery_session as local_has_active_discovery_session,
    poll_session_until_terminal,
    summarize_issue_creation,
)

from aws_runtime import acquire_discovery_lock, has_active_discovery_session, load_runtime_settings, release_discovery_lock


def _launch_discovery_session(settings: dict, max_findings: int) -> dict:
    payload = {
        "title": f"Discover remediation candidates in {settings['repo']}",
        "prompt": build_discovery_prompt(settings["owner"], settings["repo"], settings["repo_url"], max_findings),
        "advanced_mode": "analyze",
        "repos": [settings["repo_url"]],
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
        rejected = structured.get("rejected_findings") or []
        buckets = summarize_issue_creation(findings)

        return {
            "action": "completed",
            "generated_at": utc_now(),
            "session_id": session["session_id"],
            "session_url": session["url"],
            "status": final_session["status"],
            "summary": structured.get("summary", ""),
            "findings_count": len(findings),
            "issues_opened_by_devin": buckets["opened"],
            "issues_skipped_as_duplicate": buckets["duplicate_skipped"],
            "issue_creation_failures": buckets["failed"],
            "findings_missing_issue_status": buckets["missing"],
            "rejected_findings": rejected,
        }
    finally:
        release_discovery_lock(settings)
