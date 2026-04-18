from __future__ import annotations

import json

from aws_runtime import (
    build_work_item_for_remediation,
    count_active_remediation_sessions,
    enqueue_work_item,
    ensure_tracking_issue,
    has_active_remediation_session_for_issue,
    launch_remediation_session,
    load_runtime_settings,
)
from common import slugify


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    results = []
    for record in event.get("Records", []):
        payload = json.loads(record["body"])
        is_comment_follow_up = payload.get("source", {}).get("type") in {"github_issue_comment", "github_pr_comment"}
        if payload.get("event_phase") == "raw" or "automation_decision" not in payload:
            work_item, ignored = build_work_item_for_remediation(settings, payload)
            if ignored:
                results.append(
                    {
                        "source": payload["source"]["id"],
                        "action": "ignored_non_actionable" if is_comment_follow_up else "ignored_non_security",
                    }
                )
                continue
            issue = ensure_tracking_issue(settings, work_item)
            work_item["canonical_issue_number"] = issue["number"]
            work_item["canonical_issue_url"] = issue["url"]
        else:
            work_item = payload
            is_comment_follow_up = work_item.get("source", {}).get("type") in {"github_issue_comment", "github_pr_comment"}

        if has_active_remediation_session_for_issue(settings, work_item["canonical_issue_number"]):
            if is_comment_follow_up:
                enqueue_work_item(settings, work_item)
                results.append(
                    {
                        "issue": work_item["canonical_issue_number"],
                        "action": "follow_up_requeued_for_active_session",
                    }
                )
            else:
                results.append({"issue": work_item["canonical_issue_number"], "action": "duplicate_active_session_skipped"})
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
                "action": "remediation_follow_up" if is_comment_follow_up else "launched",
                "session_id": session["session_id"],
                "family_key": slugify(work_item["family_key"]),
            }
        )
    return {"processed": results}
