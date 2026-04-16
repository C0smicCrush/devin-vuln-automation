from __future__ import annotations

import json

from aws_runtime import list_project_sessions, load_runtime_settings, snapshot_path, store_metrics_snapshot
from common import devin_request, github_request, json_load, json_dump, utc_now


def _extract_issue_number(tags: list[str]) -> int | None:
    for tag in tags:
        if tag.startswith("issue:"):
            try:
                return int(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _load_previous_snapshot() -> dict:
    return json_load(snapshot_path("poller_snapshot.json"), default={"sessions": []})


def _save_snapshot(snapshot: dict) -> None:
    json_dump(snapshot_path("poller_snapshot.json"), snapshot)


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    previous = _load_previous_snapshot()
    previous_by_session = {item["session_id"]: item for item in previous.get("sessions", [])}
    sessions = list_project_sessions(settings, phase="remediation")

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

    for summary in sessions:
        session = devin_request(
            "GET",
            f"/v3/organizations/{settings['devin_org_id']}/sessions/{summary['session_id']}",
            api_key=settings["devin_api_key"],
        )
        issue_number = _extract_issue_number(session.get("tags", []))
        if not issue_number:
            continue

        previous_item = previous_by_session.get(session["session_id"], {})
        first_pr = (session.get("pull_requests") or [{}])[0].get("pr_url")
        old_pr = (previous_item.get("pull_requests") or [{}])[0].get("pr_url")
        if session["status"] != previous_item.get("status") or first_pr != old_pr:
            lines = [
                "AWS poller status update.",
                "",
                f"- Session ID: `{session['session_id']}`",
                f"- Status: `{session['status']}`",
            ]
            if session.get("status_detail"):
                lines.append(f"- Detail: `{session['status_detail']}`")
            if first_pr:
                lines.append(f"- Pull request: {first_pr}")
            if session.get("structured_output"):
                summary_text = session["structured_output"].get("summary")
                if summary_text:
                    lines.append(f"- Summary: {summary_text}")
            github_request(
                "POST",
                f"/repos/{settings['owner']}/{settings['repo']}/issues/{issue_number}/comments",
                token=settings["gh_token"],
                payload={"body": "\n".join(lines)},
            )

        metrics["total_sessions"] += 1
        if session["status"] in {"new", "creating", "claimed", "running", "resuming"}:
            metrics["active_sessions"] += 1
        elif session["status"] == "exit":
            metrics["completed_sessions"] += 1
        elif session["status"] == "suspended":
            metrics["blocked_sessions"] += 1
        elif session["status"] == "error":
            metrics["failed_sessions"] += 1
        metrics["pull_requests_opened"] += len(session.get("pull_requests", []))
        metrics["sessions"].append(
            {
                "issue_number": issue_number,
                "session_id": session["session_id"],
                "status": session["status"],
                "status_detail": session.get("status_detail"),
                "pull_requests": session.get("pull_requests", []),
                "structured_output": session.get("structured_output"),
            }
        )

    store_metrics_snapshot(settings, metrics)
    _save_snapshot(metrics)
    return metrics
