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
from common import HttpStatusError, devin_request, json_load, json_dump, utc_now


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


TERMINAL_VERDICTS = {"verified", "not_fixed", "partially_fixed", "not_verified"}


def _structured_output_is_final(session: dict, phase: str | None = None) -> bool:
    status = session.get("status")
    status_detail = session.get("status_detail")
    if status in {"exit", "error", "suspended", "waiting_for_user"} or status_detail == "waiting_for_user":
        return True
    if phase == "verification":
        # Devin occasionally leaves a verification session in `status=running` long after it has
        # committed a final verdict into structured_output. The verdict enum is only written when
        # the reviewer actually commits to an answer (see the `verification` prompt), so treating
        # a non-empty terminal verdict as "final" is safe. Without this, PRs with a perfectly
        # good `verified` verdict sit in the poller with no verdict comment ever posted.
        structured = session.get("structured_output") or {}
        verdict = structured.get("verdict") or ""
        if verdict in TERMINAL_VERDICTS:
            return True
    return False


def _effective_structured_output(session: dict, phase: str | None = None) -> dict:
    if not _structured_output_is_final(session, phase):
        return {}
    return session.get("structured_output") or {}


def _structured_summary(session: dict, phase: str | None = None) -> str:
    return _effective_structured_output(session, phase).get("summary") or ""


def _structured_verdict(session: dict, phase: str | None = None) -> str:
    return _effective_structured_output(session, phase).get("verdict") or ""


def _structured_blocked_reason(session: dict, phase: str | None = None) -> str:
    return _effective_structured_output(session, phase).get("blocked_reason") or ""


def _structured_questions(session: dict, phase: str | None = None) -> list[str]:
    return list(_effective_structured_output(session, phase).get("questions_for_human") or [])


def _structured_decision_options(session: dict, phase: str | None = None) -> list[str]:
    return list(_effective_structured_output(session, phase).get("decision_options") or [])


def _structured_recommended_option(session: dict, phase: str | None = None) -> str:
    return _effective_structured_output(session, phase).get("recommended_option") or ""


def _structured_recommended_option_reason(session: dict, phase: str | None = None) -> str:
    return _effective_structured_output(session, phase).get("recommended_option_reason") or ""


def _session_changed(current: dict, previous: dict, phase: str | None = None) -> bool:
    current_pr = (current.get("pull_requests") or [{}])[0].get("pr_url")
    previous_pr = (previous.get("pull_requests") or [{}])[0].get("pr_url")
    return any(
        [
            current.get("status") != previous.get("status"),
            current.get("status_detail") != previous.get("status_detail"),
            current_pr != previous_pr,
            _structured_summary(current, phase) != _structured_summary(previous, phase),
            _structured_verdict(current, phase) != _structured_verdict(previous, phase),
            _structured_blocked_reason(current, phase) != _structured_blocked_reason(previous, phase),
            _structured_questions(current, phase) != _structured_questions(previous, phase),
            _structured_decision_options(current, phase) != _structured_decision_options(previous, phase),
            _structured_recommended_option(current, phase) != _structured_recommended_option(previous, phase),
            _structured_recommended_option_reason(current, phase) != _structured_recommended_option_reason(previous, phase),
        ]
    )


def _post_issue_comment(settings: dict, issue_number: int, body: str) -> None:
    post_issue_comment_once(settings, issue_number, body)


def _build_update_lines(session: dict, header: str, phase: str | None = None) -> list[str]:
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
    verdict = _structured_verdict(session, phase)
    if verdict:
        lines.append(f"- Verdict: `{verdict}`")
    summary_text = _structured_summary(session, phase)
    if summary_text:
        lines.append(f"- Summary: {summary_text}")
    blocked_reason = _structured_blocked_reason(session, phase)
    if blocked_reason:
        lines.append(f"- Blocked reason: {blocked_reason}")
    questions = _structured_questions(session, phase)
    if questions:
        lines.append("- Questions for human:")
        lines.extend(f"  - {question}" for question in questions)
    decision_options = _structured_decision_options(session, phase)
    if decision_options:
        lines.append("- Decision options:")
        lines.extend(f"  - {option}" for option in decision_options)
    recommended_option = _structured_recommended_option(session, phase)
    if recommended_option:
        lines.append(f"- Recommended option: {recommended_option}")
    recommended_reason = _structured_recommended_option_reason(session, phase)
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
            "structured_output": _effective_structured_output(session, phase),
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
        structured = _effective_structured_output(session, phase)
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
        if _structured_output_is_final(session, phase) and (
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


def _pr_number_from_url(pr_url: str | None) -> int | None:
    if not pr_url:
        return None
    try:
        return int(pr_url.rstrip("/").split("/")[-1])
    except ValueError:
        return None


def _previously_landed_terminal_verdict(previous_item: dict) -> str:
    structured = (previous_item or {}).get("structured_output") or {}
    verdict = structured.get("verdict") or ""
    return verdict if verdict in TERMINAL_VERDICTS else ""


def _build_terminal_verdict_index(
    verification_session_details: list[dict],
    previous_by_session: dict,
) -> dict[tuple[int, int], str]:
    """Map (issue_number, pr_number) -> terminal verdict string for any verification session that
    has already landed a terminal verdict **in a previous poller tick**. This is the "handoff is
    old news" index: once a verdict has been narrated on a prior tick, further polling chatter
    on the same issue+PR pair is silenced because the loop has been handed back to humans.

    Important subtlety: we intentionally do NOT include verdicts that only just landed on the
    current tick. That way, the very tick where verification lands its verdict still posts the
    normal remediation status comment alongside the new verification landing comment. It's every
    subsequent tick that goes silent. If we silenced on the landing tick too, we'd drop the
    final remediation state update on the way out, which is the most useful remediation comment
    of the whole run.

    If a new remediation session is later spawned for the same issue (e.g. by a human comment),
    it will be tagged with a different `pr:` (the new PR) and this index will correctly leave
    it unmuted."""
    index: dict[tuple[int, int], str] = {}
    for session in verification_session_details:
        tags = session.get("tags") or []
        issue_number = _extract_issue_number(tags)
        pr_number = _extract_pr_number(tags)
        if not issue_number or not pr_number:
            continue
        previous_item = previous_by_session.get(session.get("session_id"), {})
        prior_verdict = _previously_landed_terminal_verdict(previous_item)
        if prior_verdict:
            # First one wins; if Devin runs multiple verifications for the same PR and they
            # disagree, the earliest terminal verdict freezes the bot. That's intentional:
            # we don't want the bot to flip-flop narration if someone re-runs verification.
            index.setdefault((issue_number, pr_number), prior_verdict)
    return index


def _fetch_session(settings: dict, session_id: str) -> dict | None:
    """Fetch a single Devin session's details, returning None if the fetch fails.

    Isolated so one bad session (Devin API hiccup, archived session returning an odd shape,
    transient 5xx) doesn't take the entire poller tick down. The caller already tolerates
    missing entries because it just skips sessions it didn't get details for."""
    try:
        return devin_request(
            "GET",
            f"/v3/organizations/{settings['devin_org_id']}/sessions/{session_id}",
            api_key=settings["devin_api_key"],
        )
    except (Exception, HttpStatusError) as exc:  # noqa: BLE001 — swallow broadly; see docstring
        # HttpStatusError subclasses SystemExit (for backcompat with older callers) so plain
        # `except Exception` would leak it. List it explicitly.
        print(f"poller: failed to fetch devin session {session_id}: {type(exc).__name__}: {exc}")
        return None


def _process_remediation_session(
    settings: dict,
    session: dict,
    previous_by_session: dict,
    terminal_verdict_by_pr: dict,
    metrics: dict,
) -> None:
    issue_number = _extract_issue_number(session.get("tags", []))
    if not issue_number:
        return

    previous_item = previous_by_session.get(session["session_id"], {})
    first_pr = (session.get("pull_requests") or [{}])[0].get("pr_url")
    old_pr = (previous_item.get("pull_requests") or [{}])[0].get("pr_url")
    pr_number = _pr_number_from_url(first_pr)
    # Silence the remediation-loop chatter once verification has landed a terminal verdict
    # for this exact issue+PR. Metrics still get recorded; we just don't post another
    # "AWS poller status update" comment on the issue. The loop has been handed back to
    # humans at that point.
    remediation_handed_off = bool(
        pr_number and terminal_verdict_by_pr.get((issue_number, pr_number))
    )
    if not remediation_handed_off and _session_changed(session, previous_item, "remediation"):
        _post_issue_comment(
            settings,
            issue_number,
            "\n".join(_build_update_lines(session, "AWS poller status update.", "remediation")),
        )
    if first_pr and first_pr != old_pr and not has_verification_session_for_pr(
        settings,
        int(first_pr.rstrip("/").split("/")[-1]),
    ):
        launch_verification_session(settings, issue_number, session, first_pr)

    _record_session_metrics(metrics, session, issue_number, "remediation")


def _process_verification_session(
    settings: dict,
    session: dict,
    previous_by_session: dict,
    metrics: dict,
) -> None:
    issue_number = _extract_issue_number(session.get("tags", []))
    pr_number = _extract_pr_number(session.get("tags", []))
    if not issue_number or not pr_number:
        return

    previous_item = previous_by_session.get(session["session_id"], {})
    # Post the verification status comment unless the previous snapshot already saw the
    # same terminal verdict for this session. That means: the "landing" post fires exactly
    # once (when the verdict transitions from empty/provisional to terminal). Subsequent
    # polls where the verdict is unchanged are silent, even if other fields (summary,
    # status_detail) wiggle around. A legitimate verdict revision (`partially_fixed` ->
    # `verified`) would change the verdict string and still post, because the previous
    # terminal verdict no longer matches the current one.
    current_structured = _effective_structured_output(session, "verification")
    current_verdict = current_structured.get("verdict") or ""
    previous_terminal_verdict = _previously_landed_terminal_verdict(previous_item)
    already_narrated_this_verdict = bool(
        current_verdict in TERMINAL_VERDICTS
        and previous_terminal_verdict == current_verdict
    )
    if not already_narrated_this_verdict and _session_changed(session, previous_item, "verification"):
        body = "\n".join(_build_update_lines(session, "AWS verification status update.", "verification"))
        _post_issue_comment(settings, issue_number, body)
        _post_issue_comment(settings, pr_number, body)

    _record_session_metrics(metrics, session, issue_number, "verification")


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

    # Fetch all verification session details up front so we can (a) build the terminal-verdict
    # index used by both loops and (b) avoid re-fetching the same sessions twice. Per-session
    # fetch errors are tolerated so one bad session doesn't take the whole tick down.
    verification_details: list[dict] = []
    for summary in verification_sessions:
        session = _fetch_session(settings, summary["session_id"])
        if session is not None:
            verification_details.append(session)
    terminal_verdict_by_pr = _build_terminal_verdict_index(verification_details, previous_by_session)

    for summary in remediation_sessions:
        session = _fetch_session(settings, summary["session_id"])
        if session is None:
            continue
        try:
            _process_remediation_session(
                settings, session, previous_by_session, terminal_verdict_by_pr, metrics
            )
        except (Exception, HttpStatusError) as exc:  # noqa: BLE001 — keep the tick alive; one bad session shouldn't kill the rest
            print(
                f"poller: remediation session {session.get('session_id')} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    for session in verification_details:
        try:
            _process_verification_session(settings, session, previous_by_session, metrics)
        except (Exception, HttpStatusError) as exc:  # noqa: BLE001 — keep the tick alive; see note above
            print(
                f"poller: verification session {session.get('session_id')} failed: "
                f"{type(exc).__name__}: {exc}"
            )

    metrics.update(_build_issue_rollups(metrics["sessions"]))
    store_metrics_snapshot(settings, metrics)
    _save_snapshot(metrics)
    return metrics
