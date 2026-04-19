from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from common import devin_request, github_request, json_load


ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = ROOT / "dashboard"
BUILD_DIR = ROOT / "build" / "dashboard"
CONTROL_PLANE_COMMENT_PREFIXES = (
    "AWS remediation worker launched Devin as the end-to-end remediation operator",
    "AWS remediation worker paused this item for manual review.",
    "AWS poller status update.",
    "AWS verification status update.",
    "AWS poller launched a strict post-PR Devin verification review.",
    "AWS launched a strict post-PR Devin verification review for this PR.",
    "Automation lifecycle complete",
)
IGNORED_COMMENT_LOGINS = {"devin-ai-integration"}
PULL_URL_RE = re.compile(r"/pull/(\d+)")
SESSION_ID_RE = re.compile(r"(?:Verification )?Session ID:\s*`([^`]+)`")
VERDICT_RE = re.compile(r"- Verdict:\s*`([^`]+)`")


def _metrics_path() -> Path:
    return Path(os.getenv("LOCAL_METRICS_DIR", "metrics")) / "latest.json"


def _queue_path() -> Path:
    return Path(os.getenv("LOCAL_STATE_DIR", "state")) / "queue" / "work_items.json"


def _repo_owner() -> str:
    return os.getenv("TARGET_REPO_OWNER", "C0smicCrush")


def _repo_name() -> str:
    return os.getenv("TARGET_REPO_NAME", "superset-remediation")


def _dashboard_port() -> int:
    return int(os.getenv("LOCAL_DASHBOARD_PORT", "8001"))


def _github_token() -> str:
    return os.getenv("GH_TOKEN", "")


def _devin_api_key() -> str:
    return os.getenv("DEVIN_API_KEY", "")


def _devin_org_id() -> str:
    return os.getenv("DEVIN_ORG_ID", "")


def _payload_cache_ttl() -> float:
    try:
        return max(float(os.getenv("DASHBOARD_CACHE_TTL_SECONDS", "30")), 0.0)
    except ValueError:
        return 30.0


def _live_fetch_workers() -> int:
    try:
        return max(int(os.getenv("DASHBOARD_LIVE_FETCH_WORKERS", "8")), 1)
    except ValueError:
        return 8


_PAYLOAD_CACHE: dict[str, object] = {"value": None, "expires_at": 0.0}
_PAYLOAD_CACHE_LOCK = Lock()


def _parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def _average(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _session_acus_consumed(session: dict) -> float:
    value = session.get("acus_consumed")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _extract_pr_number(pr_url: str | None) -> int | None:
    if not pr_url:
        return None
    try:
        return int(str(pr_url).rstrip("/").split("/")[-1])
    except ValueError:
        return None


def _counts_as_active_or_successful_pr(pr: dict) -> bool:
    return bool(pr.get("merged_at")) or pr.get("state") == "open"


def _extract_pr_numbers_from_text(text: str | None) -> set[int]:
    if not text:
        return set()
    return {int(match) for match in PULL_URL_RE.findall(text)}


def _issue_sort_key(issue: dict) -> tuple[int, str, int]:
    issue_number = int(issue.get("number") or 0)
    created_at = str(issue.get("created_at") or "")
    is_open = 1 if str(issue.get("state") or "").lower() == "open" else 0
    return (is_open, created_at, issue_number)


def _collect_issue_pr_numbers(issue: dict, comments: list[dict], timeline: list[dict]) -> set[int]:
    pr_numbers = set(_extract_pr_numbers_from_text(issue.get("body")))
    for comment in comments:
        pr_numbers.update(_extract_pr_numbers_from_text(comment.get("body")))
    for event in timeline:
        source_issue = (event.get("source") or {}).get("issue") or {}
        if event.get("event") == "cross-referenced" and source_issue.get("pull_request"):
            source_number = source_issue.get("number")
            if source_number:
                pr_numbers.add(int(source_number))
    return pr_numbers


def _canonicalize_issue_pr_links(
    tracked_issues: list[dict], issue_to_prs: dict[int, set[int]]
) -> dict[int, set[int]]:
    canonical_issue_to_prs: dict[int, set[int]] = defaultdict(set)
    issues_by_number = {int(issue.get("number") or 0): issue for issue in tracked_issues}
    pr_to_issue: dict[int, int] = {}

    for issue_number, pr_numbers in issue_to_prs.items():
        candidate_issue = issues_by_number.get(issue_number)
        if candidate_issue is None:
            continue
        for pr_number in pr_numbers:
            current_issue_number = pr_to_issue.get(pr_number)
            current_issue = issues_by_number.get(current_issue_number or 0)
            if current_issue is None or _issue_sort_key(candidate_issue) > _issue_sort_key(current_issue):
                pr_to_issue[pr_number] = issue_number

    for pr_number, issue_number in pr_to_issue.items():
        canonical_issue_to_prs[issue_number].add(pr_number)
    return canonical_issue_to_prs


def _extract_session_id(text: str | None) -> str | None:
    if not text:
        return None
    match = SESSION_ID_RE.search(text)
    return match.group(1) if match else None


def _extract_verdict(text: str | None) -> str:
    if not text:
        return ""
    match = VERDICT_RE.search(text)
    return match.group(1) if match else ""


def _extract_status(text: str | None) -> str:
    if not text:
        return ""
    match = re.search(r"- Status:\s*`([^`]+)`", text)
    return match.group(1) if match else ""


def _extract_status_detail(text: str | None) -> str:
    if not text:
        return ""
    match = re.search(r"- Detail:\s*`([^`]+)`", text)
    return match.group(1) if match else ""


def _extract_summary(text: str | None) -> str:
    if not text:
        return ""
    match = re.search(r"- Summary:\s*(.+)", text)
    return match.group(1).strip() if match else ""


def _comment_timestamp(comment: dict) -> str:
    return (
        str(comment.get("updated_at") or "")
        or str(comment.get("created_at") or "")
        or str(comment.get("updatedAt") or "")
        or str(comment.get("createdAt") or "")
    )


def _is_control_plane_comment(text: str | None) -> bool:
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in CONTROL_PLANE_COMMENT_PREFIXES)


def _list_issue_comments(owner: str, repo: str, issue_number: int, token: str) -> list[dict]:
    comments: list[dict] = []
    page = 1
    while True:
        batch = github_request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            token=token,
            query={"per_page": "100", "page": str(page)},
        )
        comments.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return comments


def _list_issue_timeline(owner: str, repo: str, issue_number: int, token: str) -> list[dict]:
    timeline: list[dict] = []
    page = 1
    while True:
        batch = github_request(
            "GET",
            f"/repos/{owner}/{repo}/issues/{issue_number}/timeline",
            token=token,
            query={"per_page": "100", "page": str(page)},
        )
        timeline.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return timeline


def _build_live_issue_rollup(comments: list[dict]) -> dict:
    phase_by_session: dict[str, str] = {}
    remediation_sessions: set[str] = set()
    verification_sessions: set[str] = set()
    human_followups = 0
    human_info_requested = False
    latest_verdict = ""

    for comment in comments:
        body = comment.get("body") or ""
        author_login = ((comment.get("author") or {}).get("login") or "").strip()
        session_id = _extract_session_id(body)
        verdict = _extract_verdict(body)
        if verdict:
            latest_verdict = verdict
        asks_for_human = "Questions for human:" in body or "Decision options:" in body or "Blocked reason:" in body
        if _is_control_plane_comment(body):
            if asks_for_human and verdict != "verified":
                human_info_requested = True
            elif verdict:
                human_info_requested = False

        if body.startswith("AWS remediation worker launched Devin as the end-to-end remediation operator"):
            if session_id:
                phase_by_session[session_id] = "remediation"
                remediation_sessions.add(session_id)
            continue

        if (
            body.startswith("AWS verification status update.")
            or body.startswith("AWS poller launched a strict post-PR Devin verification review.")
            or body.startswith("AWS launched a strict post-PR Devin verification review for this PR.")
        ):
            if session_id:
                phase_by_session[session_id] = "verification"
                verification_sessions.add(session_id)
            continue

        if body.startswith("AWS poller status update.") and session_id:
            if phase_by_session.get(session_id) == "verification":
                verification_sessions.add(session_id)
            else:
                phase_by_session.setdefault(session_id, "remediation")
                remediation_sessions.add(session_id)
            continue

        if author_login not in IGNORED_COMMENT_LOGINS and not _is_control_plane_comment(body):
            human_followups += 1
            human_info_requested = False

    return {
        "remediation_sessions": len(remediation_sessions),
        "verification_sessions": len(verification_sessions),
        "human_comment_followups": human_followups,
        "human_info_requested": human_info_requested,
        "latest_verdict": latest_verdict,
        "verified": latest_verdict == "verified",
    }


def _build_live_sessions(owner: str, repo: str, comments: list[dict]) -> list[dict]:
    sessions_by_id: dict[str, dict] = {}
    phase_by_session: dict[str, str] = {}

    def get_session(session_id: str, phase: str) -> dict:
        session = sessions_by_id.setdefault(
            session_id,
            {
                "phase": phase,
                "issue_number": None,
                "issue_url": None,
                "session_id": session_id,
                "devin_url": f"https://app.devin.ai/sessions/{session_id}",
                "status": "unknown",
                "status_detail": "",
                "verdict": "",
                "summary": "",
                "pull_requests": [],
                "_pull_request_numbers": set(),
                "_updated_at": "",
            },
        )
        session["phase"] = phase
        phase_by_session[session_id] = phase
        return session

    for comment in comments:
        body = comment.get("body") or ""
        session_id = _extract_session_id(body)
        if not session_id or not _is_control_plane_comment(body):
            continue

        if body.startswith("AWS verification status update.") or body.startswith(
            "AWS poller launched a strict post-PR Devin verification review."
        ) or body.startswith("AWS launched a strict post-PR Devin verification review for this PR."):
            phase = "verification"
        elif body.startswith("AWS poller status update."):
            phase = phase_by_session.get(session_id, "remediation")
        else:
            phase = "remediation"

        session = get_session(session_id, phase)
        session["status"] = _extract_status(body) or session["status"] or "unknown"
        session["status_detail"] = _extract_status_detail(body) or session["status_detail"]
        session["verdict"] = _extract_verdict(body) or session["verdict"]
        session["summary"] = _extract_summary(body) or session["summary"]
        session["_updated_at"] = _comment_timestamp(comment) or session["_updated_at"]

        for pr_number in sorted(_extract_pr_numbers_from_text(body)):
            if pr_number in session["_pull_request_numbers"]:
                continue
            session["_pull_request_numbers"].add(pr_number)
            session["pull_requests"].append(
                {
                    "url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
                    "number": pr_number,
                }
            )

    serialized: list[dict] = []
    for session in sessions_by_id.values():
        session.pop("_pull_request_numbers", None)
        serialized.append(session)
    serialized.sort(key=lambda item: item.get("_updated_at") or "", reverse=True)
    for session in serialized:
        session.pop("_updated_at", None)
    return serialized


def _derive_issue_verdict(issue_rollup: dict) -> str:
    verdict = (issue_rollup.get("latest_verdict") or "").strip()
    if verdict:
        return verdict
    if issue_rollup.get("verification_sessions", 0) > 0:
        return "not_verified"
    if issue_rollup.get("remediation_sessions", 0) > 0:
        return "not_verified"
    return "not_verified"


def _build_live_dashboard_state(owner: str, repo: str, metrics: dict) -> dict:
    token = _github_token()
    state = {
        "overview": {
            "total_sessions": 0,
            "active_sessions": 0,
            "completed_sessions": 0,
            "blocked_sessions": 0,
            "failed_sessions": 0,
            "pull_requests_opened": 0,
            "tracked_items_total": 0,
            "tracked_items_verified": 0,
            "tracked_items_verified_first_pass": 0,
            "tracked_items_needing_human_followup": 0,
            "tracked_items_with_multiple_remediation_loops": 0,
            "human_comment_followups_total": 0,
        },
        "verification_verdict_counts": {
            "verified": 0,
            "partially_fixed": 0,
            "not_fixed": 0,
            "not_verified": 0,
        },
        "recent_sessions": [],
        "issue_rollups": [],
        "repo_analytics": _build_repo_analytics(owner, repo, metrics),
    }
    if not token or not state["repo_analytics"].get("computed_from_github"):
        return state

    tracked_issues = _list_tracked_issues(owner, repo, token)
    all_sessions: list[dict] = []
    all_issue_rollups: list[dict] = []
    issue_records: list[dict] = []
    raw_issue_to_prs: dict[int, set[int]] = defaultdict(set)

    def _fetch_issue_activity(issue_number: int) -> tuple[int, list[dict], list[dict]]:
        comments = _list_issue_comments(owner, repo, issue_number, token)
        comments.sort(key=_comment_timestamp)
        timeline = _list_issue_timeline(owner, repo, issue_number, token)
        return issue_number, comments, timeline

    issue_activity: dict[int, tuple[list[dict], list[dict]]] = {}
    if tracked_issues:
        worker_count = min(_live_fetch_workers(), len(tracked_issues))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(_fetch_issue_activity, int(issue["number"]))
                for issue in tracked_issues
            ]
            for future in futures:
                num, comments, timeline = future.result()
                issue_activity[num] = (comments, timeline)

    for issue in tracked_issues:
        issue_number = int(issue["number"])
        comments, timeline = issue_activity[issue_number]
        rollup = _build_live_issue_rollup(comments)
        sessions = _build_live_sessions(owner, repo, comments)
        raw_issue_to_prs[issue_number].update(_collect_issue_pr_numbers(issue, comments, timeline))

        issue_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"
        for session in sessions:
            session["issue_number"] = issue_number
            session["issue_url"] = issue_url

        canonical_sessions_for_issue: dict[str, dict] = {}
        for session in sessions:
            canonical_sessions_for_issue.setdefault(str(session.get("phase") or "unknown"), session)

        derived_verdict = _derive_issue_verdict(rollup)
        current_human_info_requested = (
            str(issue.get("state") or "").lower() == "open"
            and derived_verdict != "verified"
            and (
                rollup.get("human_info_requested", False)
                or any(
                    session.get("status") == "waiting_for_user" or session.get("status_detail") == "waiting_for_user"
                    for session in canonical_sessions_for_issue.values()
                )
            )
        )
        latest_summary = ""
        if sessions:
            latest_summary = next((s["summary"] for s in sessions if s.get("summary")), "")

        issue_records.append(
            {
                "issue": issue,
                "issue_number": issue_number,
                "issue_url": issue_url,
                "rollup": rollup,
                "sessions": sessions,
                "derived_verdict": derived_verdict,
                "latest_summary": latest_summary,
                "current_human_info_requested": current_human_info_requested,
            }
        )
    all_issue_rollups = []
    canonical_issue_to_prs = _canonicalize_issue_pr_links(tracked_issues, raw_issue_to_prs)
    pull_request_numbers = {pr_number for pr_numbers in canonical_issue_to_prs.values() for pr_number in pr_numbers}
    for record in issue_records:
        issue = record["issue"]
        issue_number = record["issue_number"]
        rollup = record["rollup"]
        sessions = record["sessions"]
        pr_numbers = canonical_issue_to_prs.get(issue_number, set())
        all_issue_rollups.append(
            {
                "issue_number": issue_number,
                "issue_url": record["issue_url"],
                "title": issue.get("title") or "",
                "state": issue.get("state") or "",
                "remediation_sessions": rollup["remediation_sessions"],
                "verification_sessions": rollup["verification_sessions"],
                "latest_verdict": record["derived_verdict"],
                "verified": record["derived_verdict"] == "verified",
                "human_info_requested": record["current_human_info_requested"],
                "human_comment_followups": rollup["human_comment_followups"],
                "latest_summary": record["latest_summary"],
                "pull_requests": [
                    {
                        "url": f"https://github.com/{owner}/{repo}/pull/{pr_number}",
                        "number": pr_number,
                    }
                    for pr_number in sorted(pr_numbers)
                ],
                "sessions": sessions,
            }
        )
        all_sessions.extend(sessions)

    all_issue_rollups.sort(key=lambda item: item["issue_number"], reverse=True)
    all_sessions.sort(key=lambda item: (item.get("issue_number") or 0, item.get("session_id") or ""), reverse=True)

    latest_session_by_issue_phase: dict[tuple[int, str], dict] = {}
    for issue_rollup in all_issue_rollups:
        for session in issue_rollup.get("sessions", []):
            key = (int(issue_rollup["issue_number"]), str(session.get("phase") or "unknown"))
            latest_session_by_issue_phase.setdefault(key, session)

    for issue_rollup in all_issue_rollups:
        verdict = issue_rollup["latest_verdict"]
        state["verification_verdict_counts"][verdict] = state["verification_verdict_counts"].get(verdict, 0) + 1

    canonical_sessions = list(latest_session_by_issue_phase.values())
    canonical_remediation_counts = [
        1.0 if any(session.get("phase") == "remediation" for session in issue.get("sessions", [])) else 0.0
        for issue in all_issue_rollups
    ]
    canonical_total_phase_counts = [
        float(len({str(session.get("phase") or "unknown") for session in issue.get("sessions", [])}))
        for issue in all_issue_rollups
    ]
    blocked_issue_count = sum(
        1
        for issue in all_issue_rollups
        if issue.get("state") == "open" and issue.get("latest_verdict") != "verified"
    )
    active_session_count = sum(
        1
        for session in canonical_sessions
        if session.get("issue_number") in {issue["issue_number"] for issue in all_issue_rollups if issue.get("state") == "open"}
        and session.get("status") in {"running", "claimed"}
    )
    failed_session_count = sum(
        1
        for session in canonical_sessions
        if session.get("issue_number") in {issue["issue_number"] for issue in all_issue_rollups if issue.get("state") == "open"}
        and session.get("status") == "failed"
    )

    state["overview"]["total_sessions"] = len(canonical_sessions)
    state["overview"]["active_sessions"] = active_session_count
    state["overview"]["blocked_sessions"] = blocked_issue_count
    state["overview"]["failed_sessions"] = failed_session_count
    state["overview"]["completed_sessions"] = max(
        len(canonical_sessions) - active_session_count - blocked_issue_count - failed_session_count,
        0,
    )
    state["overview"]["pull_requests_opened"] = len(pull_request_numbers)
    state["overview"]["tracked_items_total"] = len(all_issue_rollups)
    state["overview"]["tracked_items_verified"] = sum(1 for issue in all_issue_rollups if issue.get("latest_verdict") == "verified")
    state["overview"]["tracked_items_verified_first_pass"] = sum(
        1
        for issue in all_issue_rollups
        if issue.get("latest_verdict") == "verified" and issue.get("human_comment_followups", 0) == 0
    )
    state["overview"]["tracked_items_needing_human_followup"] = sum(
        1 for issue in all_issue_rollups if issue.get("human_info_requested")
    )
    state["overview"]["tracked_items_with_multiple_remediation_loops"] = sum(
        1 for issue in all_issue_rollups if issue.get("remediation_sessions", 0) > 1
    )
    state["overview"]["human_comment_followups_total"] = sum(
        issue.get("human_comment_followups", 0) for issue in all_issue_rollups
    )

    state["repo_analytics"]["attempted_issues_total"] = len(all_issue_rollups)
    state["repo_analytics"]["avg_remediation_iterations"] = _average(canonical_remediation_counts)
    state["repo_analytics"]["avg_total_iterations"] = _average(canonical_total_phase_counts)

    state["recent_sessions"] = all_sessions[:12]
    state["issue_rollups"] = all_issue_rollups[:12]
    return state


def _build_daily_activity(tracked_issues: list[dict], pr_details: dict[int, dict]) -> list[dict]:
    def add_day_value(store: dict[date, dict[str, int]], value: datetime | None, key: str) -> None:
        if value is None:
            return
        bucket = store.setdefault(
            value.date(),
            {
                "issues_created": 0,
                "issues_closed": 0,
                "prs_opened": 0,
                "prs_merged": 0,
                "prs_closed_unmerged": 0,
            },
        )
        bucket[key] += 1

    daily: dict[date, dict[str, int]] = {}
    all_dates: list[date] = []
    for issue in tracked_issues:
        created_at = _parse_github_datetime(issue.get("created_at"))
        closed_at = _parse_github_datetime(issue.get("closed_at"))
        if created_at:
            all_dates.append(created_at.date())
        if closed_at:
            all_dates.append(closed_at.date())
        add_day_value(daily, created_at, "issues_created")
        add_day_value(daily, closed_at, "issues_closed")

    for pr in pr_details.values():
        created_at = _parse_github_datetime(pr.get("created_at"))
        merged_at = _parse_github_datetime(pr.get("merged_at"))
        closed_at = _parse_github_datetime(pr.get("closed_at"))
        if created_at:
            all_dates.append(created_at.date())
        if merged_at:
            all_dates.append(merged_at.date())
        if closed_at and not merged_at:
            all_dates.append(closed_at.date())
        add_day_value(daily, created_at, "prs_opened")
        add_day_value(daily, merged_at, "prs_merged")
        if closed_at and not merged_at:
            add_day_value(daily, closed_at, "prs_closed_unmerged")

    if not all_dates:
        return []

    start_day = min(all_dates)
    end_day = max(all_dates)
    points: list[dict] = []
    cursor = start_day
    while cursor <= end_day:
        counts = daily.get(
            cursor,
            {
                "issues_created": 0,
                "issues_closed": 0,
                "prs_opened": 0,
                "prs_merged": 0,
                "prs_closed_unmerged": 0,
            },
        )
        points.append({"date": cursor.isoformat(), **counts})
        cursor += timedelta(days=1)
    return points


def _list_tracked_issues(owner: str, repo: str, token: str) -> list[dict]:
    issues: list[dict] = []
    page = 1
    while True:
        batch = github_request(
            "GET",
            f"/repos/{owner}/{repo}/issues",
            token=token,
            query={"state": "all", "labels": "devin-remediate", "per_page": "100", "page": str(page)},
        )
        filtered = [item for item in batch if "pull_request" not in item]
        issues.extend(filtered)
        if len(batch) < 100:
            break
        page += 1
    return issues


def _list_devin_project_sessions() -> list[dict]:
    org_id = _devin_org_id()
    api_key = _devin_api_key()
    if not org_id or not api_key:
        return []
    payload = devin_request(
        "GET",
        f"/v3/organizations/{org_id}/sessions?first=100&tags=project%3Adevin-vuln-automation",
        api_key=api_key,
    )
    sessions = payload.get("items") or payload.get("sessions") or []
    return [session for session in sessions if not session.get("is_archived")]


def _build_repo_analytics(owner: str, repo: str, metrics: dict) -> dict:
    token = _github_token()
    analytics = {
        "tracked_issues_total": 0,
        "tracked_issues_open": 0,
        "tracked_issues_closed": 0,
        "issues_with_pr": 0,
        "issues_without_pr": 0,
        "issue_to_pr_conversion_rate": None,
        "linked_prs_total": 0,
        "linked_prs_open": 0,
        "linked_prs_merged": 0,
        "linked_prs_closed_unmerged": 0,
        "attempted_issues_total": 0,
        "avg_remediation_iterations": None,
        "avg_total_iterations": None,
        "avg_human_followups": None,
        "manual_intervention_rate": None,
        "verified_issue_rate": None,
        "avg_issue_to_first_pr_seconds": None,
        "avg_issue_to_resolution_seconds": None,
        "tracked_devin_sessions_total": 0,
        "total_devin_acus": None,
        "remediation_devin_acus": None,
        "verification_devin_acus": None,
        "computed_from_devin": False,
        "daily_activity": [],
        "computed_from_github": False,
        "error": "",
    }
    project_sessions: list[dict] = []
    try:
        project_sessions = _list_devin_project_sessions()
    except SystemExit as exc:
        analytics["error"] = str(exc)
    if project_sessions:
        analytics["computed_from_devin"] = True
        analytics["tracked_devin_sessions_total"] = len(project_sessions)
        analytics["total_devin_acus"] = sum(_session_acus_consumed(session) for session in project_sessions)
        analytics["remediation_devin_acus"] = sum(
            _session_acus_consumed(session)
            for session in project_sessions
            if "phase:remediation" in (session.get("tags") or [])
        )
        analytics["verification_devin_acus"] = sum(
            _session_acus_consumed(session)
            for session in project_sessions
            if "phase:verification" in (session.get("tags") or [])
        )
    if not token:
        return analytics

    try:
        tracked_issues = _list_tracked_issues(owner, repo, token)
    except SystemExit as exc:
        analytics["error"] = str(exc)
        return analytics
    analytics["computed_from_github"] = True
    analytics["tracked_issues_total"] = len(tracked_issues)
    analytics["tracked_issues_open"] = sum(1 for issue in tracked_issues if issue.get("state") == "open")
    analytics["tracked_issues_closed"] = sum(1 for issue in tracked_issues if issue.get("state") == "closed")

    issue_rollup_by_number = {
        issue.get("issue_number"): issue for issue in (metrics.get("issue_rollups") or []) if issue.get("issue_number")
    }

    issue_to_prs: dict[int, set[int]] = defaultdict(set)
    for session in metrics.get("sessions") or []:
        issue_number = session.get("issue_number")
        if not issue_number:
            continue
        for pr in session.get("pull_requests") or []:
            pr_number = _extract_pr_number(pr.get("pr_url"))
            if pr_number:
                issue_to_prs[int(issue_number)].add(pr_number)

    for issue in tracked_issues:
        issue_number = int(issue["number"])
        try:
            comments = _list_issue_comments(owner, repo, issue_number, token)
            timeline = _list_issue_timeline(owner, repo, issue_number, token)
        except SystemExit as exc:
            analytics["error"] = str(exc)
            analytics["computed_from_github"] = False
            return analytics

        issue_to_prs[issue_number].update(_collect_issue_pr_numbers(issue, comments, timeline))

        issue_rollup_by_number[issue_number] = {
            "issue_number": issue_number,
            **_build_live_issue_rollup(comments),
        }

    issue_to_prs = _canonicalize_issue_pr_links(tracked_issues, issue_to_prs)
    linked_pr_numbers = sorted({pr_number for numbers in issue_to_prs.values() for pr_number in numbers})
    pr_details: dict[int, dict] = {}
    for pr_number in linked_pr_numbers:
        try:
            pr_details[pr_number] = github_request(
                "GET",
                f"/repos/{owner}/{repo}/pulls/{pr_number}",
                token=token,
            )
        except SystemExit as exc:
            analytics["error"] = str(exc)
            analytics["computed_from_github"] = False
            return analytics

    analytics["linked_prs_total"] = len(pr_details)
    analytics["linked_prs_open"] = sum(1 for pr in pr_details.values() if pr.get("state") == "open")
    analytics["linked_prs_merged"] = sum(1 for pr in pr_details.values() if pr.get("merged_at"))
    analytics["linked_prs_closed_unmerged"] = sum(
        1 for pr in pr_details.values() if pr.get("state") == "closed" and not pr.get("merged_at")
    )
    analytics["daily_activity"] = _build_daily_activity(tracked_issues, pr_details)

    issues_with_pr = 0
    issue_to_first_pr_durations: list[float] = []
    issue_to_resolution_durations: list[float] = []
    remediation_iterations: list[float] = []
    total_iterations: list[float] = []
    human_followups: list[float] = []
    manual_intervention_count = 0
    verified_issue_count = 0

    for issue in tracked_issues:
        issue_number = int(issue["number"])
        linked_prs = [pr_details[number] for number in sorted(issue_to_prs.get(issue_number, set())) if number in pr_details]
        effective_linked_prs = [pr for pr in linked_prs if _counts_as_active_or_successful_pr(pr)]
        if effective_linked_prs:
            issues_with_pr += 1

        issue_created_at = _parse_github_datetime(issue.get("created_at"))
        first_pr_created_at = min(
            (
                created_at
                for created_at in (_parse_github_datetime(pr.get("created_at")) for pr in effective_linked_prs)
                if created_at is not None
            ),
            default=None,
        )
        first_pr_duration = _duration_seconds(issue_created_at, first_pr_created_at)
        if first_pr_duration is not None:
            issue_to_first_pr_durations.append(first_pr_duration)

        issue_closed_at = _parse_github_datetime(issue.get("closed_at"))
        fallback_resolution_at = min(
            (
                merged_at
                for merged_at in (_parse_github_datetime(pr.get("merged_at")) for pr in effective_linked_prs)
                if merged_at is not None
            ),
            default=None,
        )
        resolution_duration = _duration_seconds(issue_created_at, issue_closed_at or fallback_resolution_at)
        if resolution_duration is not None:
            issue_to_resolution_durations.append(resolution_duration)

        rollup = issue_rollup_by_number.get(issue_number)
        if not rollup:
            continue

        remediation_count = float(rollup.get("remediation_sessions", 0))
        verification_count = float(rollup.get("verification_sessions", 0))
        remediation_iterations.append(remediation_count)
        total_iterations.append(remediation_count + verification_count)
        human_followups.append(float(rollup.get("human_comment_followups", 0)))
        if rollup.get("human_comment_followups", 0) > 0:
            manual_intervention_count += 1
        if rollup.get("verified"):
            verified_issue_count += 1

    analytics["issues_with_pr"] = issues_with_pr
    analytics["issues_without_pr"] = analytics["tracked_issues_total"] - issues_with_pr
    analytics["issue_to_pr_conversion_rate"] = (
        issues_with_pr / analytics["tracked_issues_total"] if analytics["tracked_issues_total"] else None
    )

    analytics["attempted_issues_total"] = len(remediation_iterations)
    analytics["avg_remediation_iterations"] = _average(remediation_iterations)
    analytics["avg_total_iterations"] = _average(total_iterations)
    analytics["avg_human_followups"] = _average(human_followups)
    analytics["manual_intervention_rate"] = (
        manual_intervention_count / len(remediation_iterations) if remediation_iterations else None
    )
    analytics["verified_issue_rate"] = (
        verified_issue_count / len(remediation_iterations) if remediation_iterations else None
    )
    analytics["avg_issue_to_first_pr_seconds"] = _average(issue_to_first_pr_durations)
    analytics["avg_issue_to_resolution_seconds"] = _average(issue_to_resolution_durations)
    return analytics


def _base_payload() -> dict:
    owner = _repo_owner()
    repo = _repo_name()
    return {
        "repo": {
            "owner": owner,
            "name": repo,
            "url": f"https://github.com/{owner}/{repo}",
            "issues_url": f"https://github.com/{owner}/{repo}/issues",
            "pulls_url": f"https://github.com/{owner}/{repo}/pulls",
        },
        "generated_at": None,
        "queue_depth": 0,
        "overview": {
            "total_sessions": 0,
            "active_sessions": 0,
            "completed_sessions": 0,
            "blocked_sessions": 0,
            "failed_sessions": 0,
            "pull_requests_opened": 0,
            "tracked_items_total": 0,
            "tracked_items_verified": 0,
            "tracked_items_verified_first_pass": 0,
            "tracked_items_needing_human_followup": 0,
            "tracked_items_with_multiple_remediation_loops": 0,
            "human_comment_followups_total": 0,
        },
        "verification_verdict_counts": {
            "verified": 0,
            "partially_fixed": 0,
            "not_fixed": 0,
            "not_verified": 0,
        },
        "repo_analytics": {
            "tracked_issues_total": 0,
            "tracked_issues_open": 0,
            "tracked_issues_closed": 0,
            "issues_with_pr": 0,
            "issues_without_pr": 0,
            "issue_to_pr_conversion_rate": None,
            "linked_prs_total": 0,
            "linked_prs_open": 0,
            "linked_prs_merged": 0,
            "linked_prs_closed_unmerged": 0,
            "attempted_issues_total": 0,
            "avg_remediation_iterations": None,
            "avg_total_iterations": None,
            "avg_human_followups": None,
            "manual_intervention_rate": None,
            "verified_issue_rate": None,
            "avg_issue_to_first_pr_seconds": None,
            "avg_issue_to_resolution_seconds": None,
            "tracked_devin_sessions_total": 0,
            "total_devin_acus": None,
            "remediation_devin_acus": None,
            "verification_devin_acus": None,
            "computed_from_devin": False,
            "daily_activity": [],
            "computed_from_github": False,
            "error": "",
        },
        "recent_sessions": [],
        "issue_rollups": [],
    }


def _count_queued_work_items() -> int:
    queue = json_load(_queue_path(), default=[])
    if not isinstance(queue, list):
        return 0
    return len(queue)


def _build_session_view(session: dict, owner: str, repo: str) -> dict:
    issue_number = session.get("issue_number")
    pull_requests = []
    for pr in session.get("pull_requests") or []:
        pr_url = pr.get("pr_url")
        if not pr_url:
            continue
        pr_number = None
        try:
            pr_number = int(str(pr_url).rstrip("/").split("/")[-1])
        except ValueError:
            pr_number = None
        pull_requests.append(
            {
                "url": pr_url,
                "number": pr_number,
            }
        )

    return {
        "phase": session.get("phase") or "unknown",
        "issue_number": issue_number,
        "issue_url": f"https://github.com/{owner}/{repo}/issues/{issue_number}" if issue_number else None,
        "session_id": session.get("session_id"),
        "devin_url": (
            f"https://app.devin.ai/sessions/{session['session_id']}"
            if session.get("session_id")
            else None
        ),
        "status": session.get("status") or "unknown",
        "status_detail": session.get("status_detail") or "",
        "verdict": (session.get("structured_output") or {}).get("verdict") or "",
        "summary": (session.get("structured_output") or {}).get("summary") or "",
        "pull_requests": pull_requests,
    }


def _build_issue_rollup_view(issue: dict, owner: str, repo: str) -> dict:
    issue_number = issue.get("issue_number")
    return {
        "issue_number": issue_number,
        "issue_url": f"https://github.com/{owner}/{repo}/issues/{issue_number}" if issue_number else None,
        "remediation_sessions": issue.get("remediation_sessions", 0),
        "verification_sessions": issue.get("verification_sessions", 0),
        "latest_verdict": issue.get("latest_verdict", ""),
        "verified": bool(issue.get("verified", False)),
        "human_info_requested": bool(issue.get("human_info_requested", False)),
        "human_comment_followups": issue.get("human_comment_followups", 0),
    }


def build_dashboard_payload() -> dict:
    ttl = _payload_cache_ttl()
    with _PAYLOAD_CACHE_LOCK:
        now = time.monotonic()
        cached = _PAYLOAD_CACHE.get("value")
        expires_at = _PAYLOAD_CACHE.get("expires_at", 0.0)
        if ttl > 0 and cached is not None and isinstance(expires_at, (int, float)) and now < expires_at:
            return cached  # type: ignore[return-value]
        payload = _build_dashboard_payload_uncached()
        if ttl > 0:
            _PAYLOAD_CACHE["value"] = payload
            _PAYLOAD_CACHE["expires_at"] = time.monotonic() + ttl
        return payload


def _build_dashboard_payload_uncached() -> dict:
    owner = _repo_owner()
    repo = _repo_name()
    payload = _base_payload()
    payload["queue_depth"] = _count_queued_work_items()

    metrics = json_load(_metrics_path(), default={})
    if not isinstance(metrics, dict):
        return payload

    payload["generated_at"] = metrics.get("generated_at")

    for key in payload["overview"]:
        if key in metrics:
            payload["overview"][key] = metrics.get(key, payload["overview"][key])

    verdict_counts = metrics.get("verification_verdict_counts") or {}
    if isinstance(verdict_counts, dict):
        payload["verification_verdict_counts"].update(verdict_counts)

    sessions = metrics.get("sessions") or []
    if isinstance(sessions, list):
        payload["recent_sessions"] = [
            _build_session_view(session, owner, repo) for session in sessions[-12:]
        ][::-1]

    issue_rollups = metrics.get("issue_rollups") or []
    if isinstance(issue_rollups, list):
        payload["issue_rollups"] = [
            _build_issue_rollup_view(issue, owner, repo) for issue in issue_rollups[:12]
        ]

    live_state = _build_live_dashboard_state(owner, repo, metrics)
    payload["repo_analytics"] = live_state["repo_analytics"]
    if payload["repo_analytics"].get("computed_from_github"):
        payload["overview"].update(live_state["overview"])
        payload["verification_verdict_counts"].update(live_state["verification_verdict_counts"])
        payload["recent_sessions"] = live_state["recent_sessions"]
        payload["issue_rollups"] = live_state["issue_rollups"]
    return payload


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "devin-vuln-automation-dashboard"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"", "/"}:
            self._serve_file("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/app.js":
            self._serve_file("app.js", "application/javascript; charset=utf-8", build_only=True)
            return
        if parsed.path == "/styles.css":
            self._serve_file("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        if parsed.path == "/api/metrics":
            self._write_json(HTTPStatus.OK, build_dashboard_payload())
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _serve_file(self, name: str, content_type: str, build_only: bool = False) -> None:
        candidates = [BUILD_DIR / name] if build_only else [DASHBOARD_DIR / name, BUILD_DIR / name]
        path = next((candidate for candidate in candidates if candidate.exists()), None)
        if path is None:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": f"Missing asset: {name}"})
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    port = _dashboard_port()
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"dashboard listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
