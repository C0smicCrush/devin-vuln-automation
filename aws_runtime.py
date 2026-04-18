from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import boto3

from common import (
    build_remediation_prompt_from_work_item,
    build_verification_prompt,
    canonical_issue_body_from_work_item,
    compact_json,
    derive_family_key,
    devin_request,
    env,
    github_request,
    json_dump,
    json_load,
    load_test_tier_matrix,
    seed_work_item_from_raw,
    session_output_schema,
    slugify,
    utc_now,
    verification_output_schema,
)


secrets_client = boto3.client("secretsmanager")
sqs_client = boto3.client("sqs")
s3_client = boto3.client("s3")

CONTROL_PLANE_COMMENT_PREFIXES = (
    "AWS remediation worker launched Devin as the end-to-end remediation operator",
    "AWS remediation worker paused this item for manual review.",
    "AWS poller status update.",
    "AWS verification status update.",
    "AWS poller launched a strict post-PR Devin verification review.",
    "AWS launched a strict post-PR Devin verification review for this PR.",
)
IGNORED_COMMENT_LOGINS = {"devin-ai-integration"}


def load_runtime_settings() -> dict[str, Any]:
    secret_name = env("AWS_APP_SECRET_NAME")
    secret_value = secrets_client.get_secret_value(SecretId=secret_name)["SecretString"]
    payload = json.loads(secret_value)
    owner = os.getenv("TARGET_REPO_OWNER", payload.get("TARGET_REPO_OWNER", "C0smicCrush"))
    repo = os.getenv("TARGET_REPO_NAME", payload.get("TARGET_REPO_NAME", "superset-remediation"))
    return {
        "gh_token": payload["GH_TOKEN"],
        "devin_api_key": payload["DEVIN_API_KEY"],
        "devin_org_id": payload["DEVIN_ORG_ID"],
        "github_webhook_secret": payload.get("GITHUB_WEBHOOK_SECRET", ""),
        "linear_webhook_secret": payload.get("LINEAR_WEBHOOK_SECRET", ""),
        "queue_url": env("AWS_SQS_QUEUE_URL"),
        "metrics_bucket": os.getenv("AWS_METRICS_BUCKET", payload.get("AWS_METRICS_BUCKET", "")),
        "owner": owner,
        "repo": repo,
        "repo_url": f"https://github.com/{owner}/{repo}",
        "max_active_remediations": int(payload.get("MAX_ACTIVE_REMEDIATIONS", os.getenv("MAX_ACTIVE_REMEDIATIONS", 2))),
        "discovery_timeout_seconds": int(payload.get("DISCOVERY_TIMEOUT_SECONDS", os.getenv("DISCOVERY_TIMEOUT_SECONDS", 900))),
        "discovery_lock_ttl_seconds": int(payload.get("DISCOVERY_LOCK_TTL_SECONDS", os.getenv("DISCOVERY_LOCK_TTL_SECONDS", 5400))),
        "max_discovery_findings": int(payload.get("MAX_DISCOVERY_FINDINGS", os.getenv("MAX_DISCOVERY_FINDINGS", 1))),
        "remediation_bypass_approval": str(payload.get("DEVIN_BYPASS_APPROVAL", "false")).lower() in {"1", "true", "yes", "on"},
        "verification_bypass_approval": str(payload.get("DEVIN_VERIFICATION_BYPASS_APPROVAL", "false")).lower() in {"1", "true", "yes", "on"},
    }


def _lower_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def verify_signature(secret: str, body: str, signature: str) -> bool:
    if not secret:
        return True
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


def _extract_issue_number_from_tags(tags: list[str] | None) -> int | None:
    for tag in tags or []:
        if not tag.startswith("issue:"):
            continue
        try:
            return int(tag.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _is_tracked_issue(issue: dict[str, Any]) -> bool:
    labels = [item["name"] for item in issue.get("labels", [])]
    return "devin-remediate" in labels


def _is_automation_comment(author: dict[str, Any], body: str) -> bool:
    login = str(author.get("login", "")).lower()
    author_type = str(author.get("type", "")).lower()
    text = (body or "").strip()
    if author_type == "bot" or login in IGNORED_COMMENT_LOGINS:
        return True
    return any(text.startswith(prefix) for prefix in CONTROL_PLANE_COMMENT_PREFIXES)


def _infer_follow_up_reason(comment_body: str, is_pr_comment: bool) -> str:
    text = (comment_body or "").lower()
    if any(token in text for token in ["need", "missing", "clarify", "which", "what", "?"]):
        return "requested_info"
    if is_pr_comment and any(token in text for token in ["please", "change", "review", "feedback", "fix", "nit"]):
        return "pr_feedback"
    if any(token in text for token in ["lgtm", "looks good", "approved", "thanks", "thank you"]):
        return "acknowledgement"
    return "human_reply"


def _comment_dedupe_key(comment_id: str) -> str:
    return f"dedupe/comments/{comment_id}.json"


def register_comment_event_once(settings: dict[str, Any], comment_id: str, payload: dict[str, Any]) -> bool:
    bucket = settings.get("metrics_bucket")
    if not bucket:
        return True
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=_comment_dedupe_key(comment_id),
            Body=(json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return True
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
            return False
        raise


def _resolve_canonical_issue_for_pr(settings: dict[str, Any], pr_number: int) -> int | None:
    for phase in ("verification", "remediation"):
        for session in list_project_sessions(settings, phase=phase):
            tags = session.get("tags") or []
            issue_number = _extract_issue_number_from_tags(tags)
            if not issue_number:
                continue
            if f"pr:{pr_number}" in tags:
                return issue_number
            for pr in session.get("pull_requests") or []:
                pr_url = str(pr.get("pr_url", "")).rstrip("/")
                if pr_url.endswith(f"/{pr_number}"):
                    return issue_number
    pr = github_request(
        "GET",
        f"/repos/{settings['owner']}/{settings['repo']}/pulls/{pr_number}",
        token=settings["gh_token"],
    )
    body = pr.get("body") or ""
    matches = re.findall(r"#(\d+)", body)
    for match in matches:
        issue = github_request(
            "GET",
            f"/repos/{settings['owner']}/{settings['repo']}/issues/{match}",
            token=settings["gh_token"],
        )
        if _is_tracked_issue(issue):
            return int(match)
    return None


def _latest_verification_context(
    settings: dict[str, Any],
    *,
    canonical_issue_number: int,
    parent_pr_number: int | None = None,
) -> dict[str, Any]:
    if not settings.get("devin_org_id") or not settings.get("devin_api_key"):
        return {
            "reviewer_verdict": None,
            "reviewer_summary": None,
            "reviewer_questions": [],
            "reviewer_decision_options": [],
            "reviewer_recommended_option": None,
            "reviewer_recommended_option_reason": None,
        }
    candidate: dict[str, Any] | None = None
    candidate_created_at = -1
    issue_tag = f"issue:{canonical_issue_number}"
    pr_tag = f"pr:{parent_pr_number}" if parent_pr_number else None
    for session in list_project_sessions(settings, phase="verification"):
        tags = session.get("tags") or []
        if issue_tag not in tags:
            continue
        if pr_tag and pr_tag not in tags:
            continue
        created_at = int(session.get("created_at") or 0)
        if created_at >= candidate_created_at:
            candidate = session
            candidate_created_at = created_at
    structured = (candidate or {}).get("structured_output") or {}
    return {
        "reviewer_verdict": structured.get("verdict"),
        "reviewer_summary": structured.get("summary"),
        "reviewer_questions": list(structured.get("questions_for_human") or []),
        "reviewer_decision_options": list(structured.get("decision_options") or []),
        "reviewer_recommended_option": structured.get("recommended_option"),
        "reviewer_recommended_option_reason": structured.get("recommended_option_reason"),
    }


def _build_comment_work_item(
    *,
    settings: dict[str, Any],
    event_type: str,
    source_type: str,
    comment: dict[str, Any],
    canonical_issue: dict[str, Any],
    source_action: str,
    parent_pr_number: int | None = None,
) -> dict[str, Any]:
    comment_body = comment.get("body", "")
    follow_up_reason = _infer_follow_up_reason(comment_body, is_pr_comment=parent_pr_number is not None)
    labels = [item["name"] for item in canonical_issue.get("labels", [])]
    verification_context = _latest_verification_context(
        settings,
        canonical_issue_number=int(canonical_issue.get("number")),
        parent_pr_number=parent_pr_number,
    )
    return {
        "event_type": event_type,
        "event_phase": "raw",
        "source": {
            "type": source_type,
            "id": str(comment.get("id", "comment")),
            "url": comment.get("html_url", ""),
            "action": source_action,
        },
        "title": canonical_issue.get("title", f"Follow-up for issue #{canonical_issue.get('number')}"),
        "body": comment_body,
        "labels": labels,
        "created_at": comment.get("created_at", ""),
        "canonical_issue_number": canonical_issue.get("number"),
        "family_key": f"issue-{canonical_issue.get('number')}",
        "comment_id": str(comment.get("id", "")),
        "comment_author": str(comment.get("user", {}).get("login", "")),
        "comment_body": comment_body,
        "comment_url": comment.get("html_url", ""),
        "parent_pr_number": parent_pr_number,
        "follow_up_reason": follow_up_reason,
        **verification_context,
    }


def _extract_body(event: dict[str, Any]) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        return base64.b64decode(body).decode("utf-8")
    return body


def parse_incoming_event(event: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    headers = _lower_headers(event.get("headers"))
    body_text = _extract_body(event)
    path = (
        event.get("rawPath")
        or event.get("requestContext", {}).get("http", {}).get("path")
        or "/events/manual"
    )
    source_type = path.rsplit("/", 1)[-1] or "manual"

    if source_type == "github":
        signature = headers.get("x-hub-signature-256", "")
        if not verify_signature(settings["github_webhook_secret"], body_text, signature):
            raise SystemExit("Invalid GitHub webhook signature")
        payload = json.loads(body_text or "{}")
        github_event = headers.get("x-github-event", "")
        if github_event == "issues":
            issue = payload.get("issue", {})
            action = payload.get("action", "")
            labels = [item["name"] for item in issue.get("labels", [])]
            if action not in {"opened", "reopened", "labeled"}:
                return {"ignored": True, "reason": f"Ignored GitHub issue action: {action}"}
            if action == "labeled" and payload.get("label", {}).get("name") != "devin-remediate":
                return {"ignored": True, "reason": "Ignored GitHub label event without devin-remediate"}
            if action in {"opened", "reopened"} and "devin-remediate" not in labels:
                return {"ignored": True, "reason": "Ignored GitHub issue without devin-remediate label"}
            title = issue.get("title", "")
            body = issue.get("body", "")
            return {
                "event_type": payload.get("event_type", "github_issue"),
                "event_phase": "raw",
                "source": {
                    "type": "github_issue",
                    "id": str(issue.get("id", payload.get("delivery", "unknown"))),
                    "url": issue.get("html_url", ""),
                    "action": action,
                },
                "title": title,
                "body": body,
                "labels": labels,
                "created_at": issue.get("created_at", ""),
                "canonical_issue_number": issue.get("number"),
                "family_key": derive_family_key(title, labels),
            }
        if github_event == "issue_comment":
            action = payload.get("action", "")
            if action != "created":
                return {"ignored": True, "reason": f"Ignored GitHub issue_comment action: {action}"}
            comment = payload.get("comment", {})
            if _is_automation_comment(comment.get("user", {}), comment.get("body", "")):
                return {"ignored": True, "reason": "Ignored automation-authored issue comment"}
            issue = payload.get("issue", {})
            if issue.get("pull_request"):
                pr_number = issue.get("number")
                linked_issue_number = _resolve_canonical_issue_for_pr(settings, pr_number)
                if not linked_issue_number:
                    return {"ignored": True, "reason": "Ignored PR comment without linked tracked issue"}
                canonical_issue = github_request(
                    "GET",
                    f"/repos/{settings['owner']}/{settings['repo']}/issues/{linked_issue_number}",
                    token=settings["gh_token"],
                )
                work_item = _build_comment_work_item(
                    settings=settings,
                    event_type="github_pr_comment",
                    source_type="github_pr_comment",
                    comment=comment,
                    canonical_issue=canonical_issue,
                    source_action=action,
                    parent_pr_number=pr_number,
                )
            else:
                if not _is_tracked_issue(issue):
                    return {"ignored": True, "reason": "Ignored issue comment without devin-remediate label"}
                work_item = _build_comment_work_item(
                    settings=settings,
                    event_type="github_issue_comment",
                    source_type="github_issue_comment",
                    comment=comment,
                    canonical_issue=issue,
                    source_action=action,
                )
            if not register_comment_event_once(settings, work_item["comment_id"], work_item):
                return {"ignored": True, "reason": "Ignored duplicate comment event"}
            return work_item
        if github_event == "pull_request_review_comment":
            action = payload.get("action", "")
            if action != "created":
                return {"ignored": True, "reason": f"Ignored GitHub pull_request_review_comment action: {action}"}
            comment = payload.get("comment", {})
            if _is_automation_comment(comment.get("user", {}), comment.get("body", "")):
                return {"ignored": True, "reason": "Ignored automation-authored PR review comment"}
            pr = payload.get("pull_request", {})
            pr_number = pr.get("number")
            linked_issue_number = _resolve_canonical_issue_for_pr(settings, pr_number)
            if not linked_issue_number:
                return {"ignored": True, "reason": "Ignored PR review comment without linked tracked issue"}
            canonical_issue = github_request(
                "GET",
                f"/repos/{settings['owner']}/{settings['repo']}/issues/{linked_issue_number}",
                token=settings["gh_token"],
            )
            work_item = _build_comment_work_item(
                settings=settings,
                event_type="github_pr_comment",
                source_type="github_pr_comment",
                comment=comment,
                canonical_issue=canonical_issue,
                source_action=action,
                parent_pr_number=pr_number,
            )
            if not register_comment_event_once(settings, work_item["comment_id"], work_item):
                return {"ignored": True, "reason": "Ignored duplicate comment event"}
            return work_item
        else:
            return {"ignored": True, "reason": f"Unsupported GitHub event: {github_event}"}

    payload = json.loads(body_text or "{}")
    if source_type == "linear":
        return {
            "event_type": payload.get("event_type", "linear_ticket"),
            "event_phase": "raw",
            "source": {
                "type": "linear_ticket",
                "id": str(payload.get("id", "linear-stub")),
                "url": payload.get("url", ""),
                "action": payload.get("action", "created"),
            },
            "title": payload.get("title", "Linear ticket"),
            "body": payload.get("description", ""),
            "labels": payload.get("labels", []),
            "created_at": payload.get("created_at", ""),
            "canonical_issue_number": None,
            "family_key": derive_family_key(payload.get("title", ""), payload.get("labels", [])),
        }

    return {
        "event_type": payload.get("event_type", "manual"),
        "event_phase": "raw",
        "source": {
            "type": "manual_endpoint",
            "id": str(payload.get("id", f"manual-{utc_now()}")),
            "url": payload.get("url", ""),
            "action": payload.get("action", "submitted"),
        },
        "title": payload.get("title", "Manual work item"),
        "body": payload.get("body", ""),
        "labels": payload.get("labels", []),
        "created_at": payload.get("created_at", ""),
        "canonical_issue_number": payload.get("canonical_issue_number"),
        "family_key": payload.get("family_key") or derive_family_key(payload.get("title", ""), payload.get("labels", [])),
    }


def poll_session_until_terminal(settings: dict[str, Any], session_id: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        session = devin_request(
            "GET",
            f"/v3/organizations/{settings['devin_org_id']}/sessions/{session_id}",
            api_key=settings["devin_api_key"],
        )
        if session["status"] in {"exit", "error", "suspended"}:
            return session
        if session["status"] == "waiting_for_user" and session.get("structured_output"):
            return session
        time.sleep(10)
    raise SystemExit(f"Timed out waiting for Devin session {session_id}")


def build_work_item_for_remediation(settings: dict[str, Any], raw_work_item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    del settings
    work_item = seed_work_item_from_raw(raw_work_item, load_test_tier_matrix())
    work_item.update(
        {
            "event_type": raw_work_item.get("event_type"),
            "event_phase": raw_work_item.get("event_phase", "seeded"),
            "source": raw_work_item.get("source", {}),
            "title": raw_work_item.get("title"),
            "body": raw_work_item.get("body"),
            "labels": raw_work_item.get("labels", []),
            "created_at": raw_work_item.get("created_at") or utc_now(),
            "canonical_issue_number": raw_work_item.get("canonical_issue_number"),
            "family_key": raw_work_item.get("family_key") or work_item.get("family_key"),
            "comment_id": raw_work_item.get("comment_id"),
            "comment_author": raw_work_item.get("comment_author"),
            "comment_body": raw_work_item.get("comment_body"),
            "comment_url": raw_work_item.get("comment_url"),
            "parent_pr_number": raw_work_item.get("parent_pr_number"),
            "follow_up_reason": raw_work_item.get("follow_up_reason"),
            "reviewer_verdict": raw_work_item.get("reviewer_verdict"),
            "reviewer_summary": raw_work_item.get("reviewer_summary"),
            "reviewer_questions": raw_work_item.get("reviewer_questions") or [],
            "reviewer_decision_options": raw_work_item.get("reviewer_decision_options") or [],
            "reviewer_recommended_option": raw_work_item.get("reviewer_recommended_option"),
            "reviewer_recommended_option_reason": raw_work_item.get("reviewer_recommended_option_reason"),
        }
    )
    if not work_item.get("created_at"):
        work_item["created_at"] = utc_now()
    if not work_item.get("canonical_issue_body"):
        work_item["canonical_issue_body"] = canonical_issue_body_from_work_item(work_item)
    return work_item, False


def ensure_tracking_issue(settings: dict[str, Any], work_item: dict[str, Any]) -> dict[str, Any]:
    owner = settings["owner"]
    repo = settings["repo"]
    token = settings["gh_token"]
    issue_number = work_item.get("canonical_issue_number")
    if issue_number:
        issue = github_request("GET", f"/repos/{owner}/{repo}/issues/{issue_number}", token=token)
        return {"number": issue["number"], "url": issue["html_url"], "title": issue["title"]}

    desired_labels = list(dict.fromkeys(work_item.get("issue_labels", []) + ["devin-remediate", "aws-event-driven"]))
    existing_labels = github_request(
        "GET",
        f"/repos/{owner}/{repo}/labels",
        token=token,
        query={"per_page": "100"},
    )
    existing_names = {item["name"] for item in existing_labels}
    palette = {
        "security-remediation": ("d73a4a", "Scanner-derived security work item"),
        "devin-remediate": ("0e8a16", "Trigger Devin remediation from this finding"),
        "aws-event-driven": ("1d76db", "Managed by the AWS event-driven remediation pipeline"),
        "manual-source": ("5319e7", "Submitted through the manual intake endpoint"),
    }
    for label in desired_labels:
        if label in existing_names:
            continue
        color, description = palette.get(label, ("1d76db", "Automation-managed label"))
        github_request(
            "POST",
            f"/repos/{owner}/{repo}/labels",
            token=token,
            payload={"name": label, "color": color, "description": description},
        )

    payload = {
        "title": work_item["canonical_issue_title"],
        "body": work_item.get("canonical_issue_body") or canonical_issue_body_from_work_item(work_item),
        "labels": desired_labels,
    }
    issue = github_request("POST", f"/repos/{owner}/{repo}/issues", token=token, payload=payload)
    return {"number": issue["number"], "url": issue["html_url"], "title": issue["title"]}


def post_issue_comment_once(settings: dict[str, Any], issue_number: int, body: str) -> bool:
    owner = settings["owner"]
    repo = settings["repo"]
    token = settings["gh_token"]
    existing_comments = github_request(
        "GET",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
        query={"per_page": "100"},
    )
    normalized_body = (body or "").strip()
    for comment in reversed(existing_comments):
        if (comment.get("body") or "").strip() == normalized_body:
            return False
    github_request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
        payload={"body": body},
    )
    return True


def enqueue_work_item(
    settings: dict[str, Any],
    work_item: dict[str, Any],
) -> dict[str, Any]:
    message_body = compact_json(work_item)
    attributes = {
        "source_type": {"DataType": "String", "StringValue": work_item["source"]["type"]},
    }
    if work_item.get("scope_tier"):
        attributes["scope_tier"] = {"DataType": "String", "StringValue": work_item["scope_tier"]}
    if work_item.get("automation_decision"):
        attributes["automation_decision"] = {"DataType": "String", "StringValue": work_item["automation_decision"]}
    if work_item.get("event_phase"):
        attributes["event_phase"] = {"DataType": "String", "StringValue": work_item["event_phase"]}
    response = sqs_client.send_message(
        QueueUrl=settings["queue_url"],
        MessageBody=message_body,
        MessageGroupId=slugify(work_item["family_key"])[:128] or "generic",
        MessageAttributes=attributes,
    )
    return {"message_id": response["MessageId"]}


def list_project_sessions(settings: dict[str, Any], phase: str | None = None) -> list[dict[str, Any]]:
    params = ["first=100", "tags=project%3Adevin-vuln-automation"]
    if phase:
        params.append(f"tags=phase%3A{phase}")
    payload = devin_request(
        "GET",
        f"/v3/organizations/{settings['devin_org_id']}/sessions?{'&'.join(params)}",
        api_key=settings["devin_api_key"],
    )
    return payload.get("items") or payload.get("sessions") or []


def has_active_discovery_session(settings: dict[str, Any]) -> bool:
    for session in list_project_sessions(settings, phase="discovery"):
        if session.get("status") in {"new", "creating", "claimed", "running", "resuming", "waiting_for_user"}:
            return True
    return False


def count_active_remediation_sessions(settings: dict[str, Any]) -> int:
    count = 0
    for session in list_project_sessions(settings, phase="remediation"):
        if session.get("status") in {"new", "creating", "claimed", "running", "resuming"}:
            count += 1
    return count


def has_active_remediation_session_for_issue(settings: dict[str, Any], issue_number: int) -> bool:
    issue_tag = f"issue:{issue_number}"
    for session in list_project_sessions(settings, phase="remediation"):
        if issue_tag not in (session.get("tags") or []):
            continue
        if session.get("status") in {"new", "creating", "claimed", "running", "resuming"}:
            return True
    return False


def has_verification_session_for_pr(settings: dict[str, Any], pr_number: int) -> bool:
    pr_tag = f"pr:{pr_number}"
    for session in list_project_sessions(settings, phase="verification"):
        if pr_tag not in (session.get("tags") or []):
            continue
        if session.get("status") in {"new", "creating", "claimed", "running", "resuming", "waiting_for_user"}:
            return True
        return True
    return False


def _discovery_lock_key() -> str:
    return "locks/discovery.lock"


def acquire_discovery_lock(settings: dict[str, Any], holder: str, ttl_seconds: int) -> bool:
    bucket = settings.get("metrics_bucket")
    if not bucket:
        return True
    key = _discovery_lock_key()
    payload = json.dumps(
        {
            "holder": holder,
            "created_at": utc_now(),
            "expires_at": utc_now() + ttl_seconds,
        },
        sort_keys=True,
    ).encode("utf-8")
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return True
    except Exception as exc:  # boto3/botocore may not be importable in local unit tests
        response = getattr(exc, "response", {}) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        if code not in {"PreconditionFailed", "ConditionalRequestConflict"}:
            raise
    try:
        current = s3_client.get_object(Bucket=bucket, Key=key)
        lock_payload = json.loads(current["Body"].read().decode("utf-8"))
    except Exception:
        return False
    if int(lock_payload.get("expires_at", 0)) > utc_now():
        return False
    s3_client.delete_object(Bucket=bucket, Key=key)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=payload,
        ContentType="application/json",
        IfNoneMatch="*",
    )
    return True


def release_discovery_lock(settings: dict[str, Any]) -> None:
    bucket = settings.get("metrics_bucket")
    if not bucket:
        return
    try:
        s3_client.delete_object(Bucket=bucket, Key=_discovery_lock_key())
    except Exception:
        return


def launch_remediation_session(settings: dict[str, Any], work_item: dict[str, Any]) -> dict[str, Any]:
    owner = settings["owner"]
    repo = settings["repo"]
    token = settings["gh_token"]
    issue = github_request(
        "GET",
        f"/repos/{owner}/{repo}/issues/{work_item['canonical_issue_number']}",
        token=token,
    )
    prompt = build_remediation_prompt_from_work_item(owner, repo, issue, work_item, settings["repo_url"])
    payload = {
        "title": f"Remediate #{issue['number']} {issue['title'][:80]}",
        "prompt": prompt,
        "advanced_mode": "create",
        "repos": [settings["repo_url"]],
        "bypass_approval": settings["remediation_bypass_approval"],
        "structured_output_schema": session_output_schema(),
        "tags": [
            "project:devin-vuln-automation",
            "phase:remediation",
            f"issue:{issue['number']}",
            f"family:{slugify(work_item['family_key'])}",
            f"scope:{work_item['scope_tier']}",
            f"source:{work_item['source'].get('type', 'unknown')}",
        ],
    }
    if work_item.get("comment_id"):
        payload["tags"].append(f"comment:{work_item['comment_id']}")
        payload["tags"].append("trigger:comment_follow_up")
    if work_item.get("parent_pr_number"):
        payload["tags"].append(f"parent_pr:{work_item['parent_pr_number']}")
    session = devin_request(
        "POST",
        f"/v3/organizations/{settings['devin_org_id']}/sessions",
        api_key=settings["devin_api_key"],
        payload=payload,
    )
    post_issue_comment_once(
        settings,
        issue["number"],
        (
            "AWS remediation worker launched Devin as the end-to-end remediation operator for this work item.\n\n"
            f"- Session ID: `{session['session_id']}`\n"
            f"- Scope tier: `{work_item['scope_tier']}`\n"
            f"- Automation decision: `{work_item['automation_decision']}`\n"
            f"- Session URL: {session['url']}\n\n"
            "Devin must attach before/after scanner receipts and test outcomes to the resulting PR. "
            "If the sandbox cannot run a required command, the PR must state exactly which one and why."
        ),
    )
    return session


def launch_verification_session(
    settings: dict[str, Any],
    issue_number: int,
    remediation_session: dict[str, Any],
    pr_url: str,
) -> dict[str, Any]:
    owner = settings["owner"]
    repo = settings["repo"]
    token = settings["gh_token"]
    pr_number = int(pr_url.rstrip("/").split("/")[-1])
    issue = github_request(
        "GET",
        f"/repos/{owner}/{repo}/issues/{issue_number}",
        token=token,
    )
    pr = github_request(
        "GET",
        f"/repos/{owner}/{repo}/pulls/{pr_number}",
        token=token,
    )
    prompt = build_verification_prompt(
        owner,
        repo,
        issue,
        pr,
        remediation_session.get("structured_output") or {},
        settings["repo_url"],
    )
    payload = {
        "title": f"Verify PR #{pr_number} for issue #{issue_number}",
        "prompt": prompt,
        "advanced_mode": "analyze",
        "repos": [settings["repo_url"]],
        "bypass_approval": settings["verification_bypass_approval"],
        "structured_output_schema": verification_output_schema(),
        "tags": [
            "project:devin-vuln-automation",
            "phase:verification",
            f"issue:{issue_number}",
            f"pr:{pr_number}",
            f"remediation_session:{remediation_session['session_id']}",
        ],
    }
    session = devin_request(
        "POST",
        f"/v3/organizations/{settings['devin_org_id']}/sessions",
        api_key=settings["devin_api_key"],
        payload=payload,
    )
    post_issue_comment_once(
        settings,
        issue_number,
        (
            "AWS poller launched a strict post-PR Devin verification review.\n\n"
            f"- PR: {pr_url}\n"
            f"- Verification session ID: `{session['session_id']}`\n"
            f"- Verification session URL: {session['url']}\n\n"
            "This reviewer is expected to act like a skeptical senior engineer: independently test whether the PR actually fixes the issue, "
            "re-run the narrowest credible validation, and call out any mismatch between the PR's claims and repository reality."
        ),
    )
    post_issue_comment_once(
        settings,
        pr_number,
        (
            "AWS launched a strict post-PR Devin verification review for this PR.\n\n"
            f"- Verification session ID: `{session['session_id']}`\n"
            f"- Verification session URL: {session['url']}\n"
            f"- Linked issue: #{issue_number}\n\n"
            "This reviewer is expected to independently verify whether the PR actually fixes the issue, "
            "re-run the narrowest credible validation, and call out any mismatch between the PR's claims and repository reality."
        ),
    )
    return session


def store_metrics_snapshot(settings: dict[str, Any], payload: dict[str, Any]) -> None:
    bucket = settings.get("metrics_bucket")
    if not bucket:
        return
    s3_client.put_object(
        Bucket=bucket,
        Key="reports/latest.json",
        Body=(json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        ContentType="application/json",
    )


def snapshot_path(name: str) -> Path:
    return Path("/tmp") / name
