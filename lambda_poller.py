from __future__ import annotations

import json

from aws_runtime import (
    has_verification_session_for_pr,
    launch_verification_session,
    list_project_sessions,
    load_runtime_settings,
    post_issue_comment_once,
    snapshot_path,
    store_metrics_snapshot,
)
from common import devin_request, json_load, json_dump, utc_now


def _extract_issue_number(tags: list[str]) -> int | None:
    for tag in tags:
        if tag.startswith("issue:"):
            try:
                return int(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _extract_pr_number(tags: list[str]) -> int | None:
    for tag in tags:
        if tag.startswith("pr:"):
            try:
                return int(tag.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _load_previous_snapshot() -> dict:
    return json_load(snapshot_path("poller_snapshot.json"), default={"sessions": []})


def _save_snapshot(snapshot: dict) -> None:
    json_dump(snapshot_path("poller_snapshot.json"), snapshot)


def _structured_output_is_final(session: dict) -> bool:
    status = session.get("status")
    status_detail = session.get("status_detail")
    return status in {"exit", "error", "suspended", "waiting_for_user"} or status_detail == "waiting_for_user"


def _effective_structured_output(session: dict) -> dict:
    if not _structured_output_is_final(session):
        return {}
    return session.get("structured_output") or {}


def _structured_summary(session: dict) -> str:
    structured = _effective_structured_output(session)
    return structured.get("summary") or ""


def _structured_verdict(session: dict) -> str:
    structured = _effective_structured_output(session)
    return structured.get("verdict") or ""


def _structured_blocked_reason(session: dict) -> str:
    structured = _effective_structured_output(session)
    return structured.get("blocked_reason") or ""


def _structured_questions(session: dict) -> list[str]:
    structured = _effective_structured_output(session)
    return list(structured.get("questions_for_human") or [])


def _structured_decision_options(session: dict) -> list[str]:
    structured = _effective_structured_output(session)
    return list(structured.get("decision_options") or [])


def _structured_recommended_option(session: dict) -> str:
    structured = _effective_structured_output(session)
    return structured.get("recommended_option") or ""


def _structured_recommended_option_reason(session: dict) -> str:
    structured = _effective_structured_output(session)
    return structured.get("recommended_option_reason") or ""


def _session_changed(current: dict, previous: dict) -> bool:
    current_pr = (current.get("pull_requests") or [{}])[0].get("pr_url")
    previous_pr = (previous.get("pull_requests") or [{}])[0].get("pr_url")
    return any(
        [
            current.get("status") != previous.get("status"),
            current.get("status_detail") != previous.get("status_detail"),
            current_pr != previous_pr,
            _structured_summary(current) != _structured_summary(previous),
            _structured_verdict(current) != _structured_verdict(previous),
            _structured_blocked_reason(current) != _structured_blocked_reason(previous),
            _structured_questions(current) != _structured_questions(previous),
            _structured_decision_options(current) != _structured_decision_options(previous),
            _structured_recommended_option(current) != _structured_recommended_option(previous),
            _structured_recommended_option_reason(current) != _structured_recommended_option_reason(previous),
        ]
    )


def _post_issue_comment(settings: dict, issue_number: int, body: str) -> None:
    post_issue_comment_once(settings, issue_number, body)


def _build_update_lines(session: dict, header: str) -> list[str]:
    lines = [
        header,
        "",
        f"- Session ID: `{session['session_id']}`",
        f"- Status: `{session['status']}`",
    ]
    if session.get("status_detail"):
        lines.append(f"- Detail: `{session['status_detail']}`")
    first_pr = (session.get("pull_requests") or [{}])[0].get("pr_url")
    if first_pr:
        lines.append(f"- Pull request: {first_pr}")
    verdict = _structured_verdict(session)
    if verdict:
        lines.append(f"- Verdict: `{verdict}`")
    summary_text = _structured_summary(session)
    if summary_text:
        lines.append(f"- Summary: {summary_text}")
    blocked_reason = _structured_blocked_reason(session)
    if blocked_reason:
        lines.append(f"- Blocked reason: {blocked_reason}")
    questions = _structured_questions(session)
    if questions:
        lines.append("- Questions for human:")
        lines.extend(f"  - {question}" for question in questions)
    decision_options = _structured_decision_options(session)
    if decision_options:
        lines.append("- Decision options:")
        lines.extend(f"  - {option}" for option in decision_options)
    recommended_option = _structured_recommended_option(session)
    if recommended_option:
        lines.append(f"- Recommended option: {recommended_option}")
    recommended_reason = _structured_recommended_option_reason(session)
    if recommended_reason:
        lines.append(f"- Recommended option reason: {recommended_reason}")
    return lines


def _record_session_metrics(metrics: dict, session: dict, issue_number: int, phase: str) -> None:
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
            "phase": phase,
            "issue_number": issue_number,
            "session_id": session["session_id"],
            "status": session["status"],
            "status_detail": session.get("status_detail"),
            "pull_requests": session.get("pull_requests", []),
            "structured_output": _effective_structured_output(session),
            "tags": session.get("tags", []),
        }
    )


def _build_issue_rollups(sessions: list[dict]) -> dict:
    verdict_counts = {
        "verified": 0,
        "partially_fixed": 0,
        "not_fixed": 0,
        "not_verified": 0,
    }
    issues: dict[int, dict] = {}
    all_comment_ids: set[str] = set()
    verification_sessions_total = 0

    for session in sessions:
        issue_number = session.get("issue_number")
        if not issue_number:
            continue
        issue = issues.setdefault(
            issue_number,
            {
                "issue_number": issue_number,
                "remediation_sessions": 0,
                "verification_sessions": 0,
                "latest_verdict": "",
                "verified": False,
                "human_info_requested": False,
                "human_comment_followups": 0,
                "comment_ids": set(),
            },
        )
        phase = session.get("phase")
        tags = session.get("tags") or []
        structured = _effective_structured_output(session)
        if phase == "remediation":
            issue["remediation_sessions"] += 1
        elif phase == "verification":
            issue["verification_sessions"] += 1
            verification_sessions_total += 1
            verdict = structured.get("verdict") or ""
            if verdict:
                issue["latest_verdict"] = verdict
                issue["verified"] = issue["verified"] or verdict == "verified"
                if verdict in verdict_counts:
                    verdict_counts[verdict] += 1
        if _structured_output_is_final(session) and (
            session.get("status") == "waiting_for_user"
            or session.get("status_detail") == "waiting_for_user"
            or structured.get("blocked_reason")
            or structured.get("questions_for_human")
        ):
            issue["human_info_requested"] = True
        for tag in tags:
            if not tag.startswith("comment:"):
                continue
            comment_id = tag.split(":", 1)[1]
            issue["comment_ids"].add(comment_id)
            all_comment_ids.add(comment_id)

    serialized_issues = []
    tracked_items_verified = 0
    tracked_items_verified_first_pass = 0
    tracked_items_needing_human_followup = 0
    tracked_items_with_multiple_remediation_loops = 0
    for issue_number in sorted(issues):
        issue = issues[issue_number]
        issue["human_comment_followups"] = len(issue["comment_ids"])
        issue["comment_ids"] = sorted(issue["comment_ids"])
        if issue["verified"]:
            tracked_items_verified += 1
            if issue["human_comment_followups"] == 0:
                tracked_items_verified_first_pass += 1
        if issue["human_info_requested"] and issue["human_comment_followups"] > 0:
            tracked_items_needing_human_followup += 1
        if issue["remediation_sessions"] > 1:
            tracked_items_with_multiple_remediation_loops += 1
        serialized_issues.append(issue)

    return {
        "tracked_items_total": len(serialized_issues),
        "tracked_items_verified": tracked_items_verified,
        "tracked_items_verified_first_pass": tracked_items_verified_first_pass,
        "tracked_items_needing_human_followup": tracked_items_needing_human_followup,
        "tracked_items_with_multiple_remediation_loops": tracked_items_with_multiple_remediation_loops,
        "human_comment_followups_total": len(all_comment_ids),
        "verification_sessions_total": verification_sessions_total,
        "verification_verdict_counts": verdict_counts,
        "issue_rollups": serialized_issues,
    }


def handler(event, context):  # noqa: ANN001
    settings = load_runtime_settings()
    previous = _load_previous_snapshot()
    previous_by_session = {item["session_id"]: item for item in previous.get("sessions", [])}
    remediation_sessions = list_project_sessions(settings, phase="remediation")
    verification_sessions = list_project_sessions(settings, phase="verification")

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

    for summary in remediation_sessions:
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
        if _session_changed(session, previous_item):
            _post_issue_comment(settings, issue_number, "\n".join(_build_update_lines(session, "AWS poller status update.")))
        if first_pr and first_pr != old_pr and not has_verification_session_for_pr(
            settings,
            int(first_pr.rstrip("/").split("/")[-1]),
        ):
            launch_verification_session(settings, issue_number, session, first_pr)

        _record_session_metrics(metrics, session, issue_number, "remediation")

    for summary in verification_sessions:
        session = devin_request(
            "GET",
            f"/v3/organizations/{settings['devin_org_id']}/sessions/{summary['session_id']}",
            api_key=settings["devin_api_key"],
        )
        issue_number = _extract_issue_number(session.get("tags", []))
        pr_number = _extract_pr_number(session.get("tags", []))
        if not issue_number or not pr_number:
            continue

        previous_item = previous_by_session.get(session["session_id"], {})
        if _session_changed(session, previous_item):
            body = "\n".join(_build_update_lines(session, "AWS verification status update."))
            _post_issue_comment(settings, issue_number, body)
            _post_issue_comment(settings, pr_number, body)

        _record_session_metrics(metrics, session, issue_number, "verification")

    metrics.update(_build_issue_rollups(metrics["sessions"]))
    store_metrics_snapshot(settings, metrics)
    _save_snapshot(metrics)
    return metrics
