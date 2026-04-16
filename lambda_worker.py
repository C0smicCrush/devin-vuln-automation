from __future__ import annotations

import json

from aws_runtime import (
    count_active_remediation_sessions,
    enqueue_work_item,
    ensure_tracking_issue,
    launch_remediation_session,
    load_runtime_settings,
    normalize_with_devin,
)
from common import canonical_issue_body_from_work_item, github_request, slugify, utc_now


def _mark_manual_review(settings: dict, work_item: dict) -> None:
    owner = settings["owner"]
    repo = settings["repo"]
    issue_number = work_item["canonical_issue_number"]
    token = settings["gh_token"]
    github_request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
        payload={
            "body": (
                "AWS remediation worker paused this item for manual review.\n\n"
                f"- Scope tier: `{work_item['scope_tier']}`\n"
                f"- Confidence: `{work_item['confidence']}`\n"
                "- The normalization step marked this work item as requiring human approval."
            )
        },
    )


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    results = []
    for record in event.get("Records", []):
        payload = json.loads(record["body"])
        if payload.get("event_phase") == "raw" or "automation_decision" not in payload:
            normalized = normalize_with_devin(settings, payload)
            if not normalized.get("is_security_related", True):
                results.append({"source": payload["source"]["id"], "action": "ignored_non_security"})
                continue
            work_item = {
                **normalized,
                "event_phase": "normalized",
                "source": payload["source"],
                "title": payload["title"],
                "body": payload["body"],
                "labels": payload.get("labels", []),
                "created_at": payload.get("created_at") or utc_now(),
                "canonical_issue_number": payload.get("canonical_issue_number"),
                "family_key": normalized.get("family_key") or payload.get("family_key"),
            }
            if not work_item.get("canonical_issue_body"):
                work_item["canonical_issue_body"] = canonical_issue_body_from_work_item(work_item)
            issue = ensure_tracking_issue(settings, work_item)
            work_item["canonical_issue_number"] = issue["number"]
            work_item["canonical_issue_url"] = issue["url"]
        else:
            work_item = payload

        if work_item["automation_decision"] != "auto":
            _mark_manual_review(settings, work_item)
            results.append({"issue": work_item["canonical_issue_number"], "action": "manual_review"})
            continue

        active = count_active_remediation_sessions(settings)
        if active >= settings["max_active_remediations"]:
            enqueue_work_item(settings, work_item)
            results.append({"issue": work_item["canonical_issue_number"], "action": "requeued"})
            continue

        session = launch_remediation_session(settings, work_item)
        results.append(
            {
                "issue": work_item["canonical_issue_number"],
                "action": "launched",
                "session_id": session["session_id"],
                "family_key": slugify(work_item["family_key"]),
            }
        )
    return {"processed": results}
