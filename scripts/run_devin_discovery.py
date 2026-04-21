from __future__ import annotations

import argparse
import time
from pathlib import Path

from common import (
    ROOT,
    build_discovery_prompt,
    default_repo_config,
    devin_request,
    discovery_output_schema,
    env,
    json_dump,
    print_json,
    slugify,
    utc_now,
)

ACTIVE_STATUSES = {"new", "creating", "claimed", "running", "resuming", "waiting_for_user"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a bounded Devin discovery pass. Devin opens the tracked issue(s) itself; "
            "this script only launches the session, polls it, and prints what Devin reported."
        )
    )
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
    """List sessions tagged with both `project:devin-vuln-automation` and `phase:<phase>`.

    Note: the Devin sessions API accepts repeated `tags=` query params, but in practice its
    filter semantics don't AND-combine them the way callers assume — a request for
    `tags=project:X&tags=phase:discovery` can still return e.g. remediation and verification
    sessions tagged with just `project:X`. We re-filter client-side on the `phase:` tag so
    callers never see a cross-phase session. Without this, `has_active_discovery_session`
    returns True any time a remediation session is running, which causes /vuln-trigger to
    no-op with `existing_discovery_session`."""
    payload = devin_request(
        "GET",
        f"/v3/organizations/{devin_org_id}/sessions?tags=project%3Adevin-vuln-automation&tags=phase%3A{phase}&first=100",
        api_key=devin_api_key,
    )
    sessions = payload.get("items") or payload.get("sessions") or []
    phase_tag = f"phase:{phase}"
    return [session for session in sessions if phase_tag in (session.get("tags") or [])]


def has_active_discovery_session(devin_org_id: str, devin_api_key: str) -> bool:
    for session in list_project_sessions(devin_org_id, devin_api_key, "discovery"):
        if session.get("status") in ACTIVE_STATUSES:
            return True
    return False


def summarize_issue_creation(findings: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {"opened": [], "duplicate_skipped": [], "failed": [], "missing": []}
    for finding in findings:
        status = finding.get("issue_creation_status")
        record = {
            "finding_id": finding.get("id"),
            "issue_url": finding.get("issue_url"),
            "issue_number": finding.get("issue_number"),
            "issue_creation_error": finding.get("issue_creation_error"),
        }
        if status in buckets:
            buckets[status].append(record)
        else:
            buckets["missing"].append(record)
    return buckets


def main() -> None:
    args = parse_args()
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
    buckets = summarize_issue_creation(findings)

    output = {
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
    json_dump(Path(args.state_file), output)
    print_json(output)


if __name__ == "__main__":
    main()
