from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

import boto3

from common import (
    build_normalization_prompt,
    build_remediation_prompt_from_work_item,
    canonical_issue_body_from_work_item,
    compact_json,
    derive_family_key,
    devin_request,
    env,
    github_request,
    json_dump,
    json_load,
    load_test_tier_matrix,
    normalization_output_schema,
    session_output_schema,
    slugify,
    utc_now,
)


secrets_client = boto3.client("secretsmanager")
sqs_client = boto3.client("sqs")
s3_client = boto3.client("s3")


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
        "normalization_timeout_seconds": int(payload.get("NORMALIZATION_TIMEOUT_SECONDS", os.getenv("NORMALIZATION_TIMEOUT_SECONDS", 180))),
        "remediation_bypass_approval": str(payload.get("DEVIN_BYPASS_APPROVAL", "false")).lower() in {"1", "true", "yes", "on"},
    }


def _lower_headers(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (headers or {}).items()}


def verify_signature(secret: str, body: str, signature: str) -> bool:
    if not secret:
        return True
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature)


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
        if github_event != "issues":
            return {"ignored": True, "reason": f"Unsupported GitHub event: {github_event}"}
        issue = payload.get("issue", {})
        labels = [item["name"] for item in issue.get("labels", [])]
        title = issue.get("title", "")
        body = issue.get("body", "")
        return {
            "event_phase": "raw",
            "source": {
                "type": "github_issue",
                "id": str(issue.get("id", payload.get("delivery", "unknown"))),
                "url": issue.get("html_url", ""),
                "action": payload.get("action", ""),
            },
            "title": title,
            "body": body,
            "labels": labels,
            "created_at": issue.get("created_at", ""),
            "canonical_issue_number": issue.get("number"),
            "family_key": derive_family_key(title, labels),
        }

    payload = json.loads(body_text or "{}")
    if source_type == "linear":
        return {
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
        time.sleep(10)
    raise SystemExit(f"Timed out waiting for Devin session {session_id}")


def normalize_with_devin(settings: dict[str, Any], raw_work_item: dict[str, Any]) -> dict[str, Any]:
    prompt = build_normalization_prompt(raw_work_item, load_test_tier_matrix(), settings["repo_url"])
    payload = {
        "title": f"Normalize {raw_work_item['source']['type']} {raw_work_item['title'][:80]}",
        "prompt": prompt,
        "advanced_mode": "analyze",
        "repos": [settings["repo_url"]],
        "max_acu_limit": 1,
        "structured_output_schema": normalization_output_schema(),
        "tags": [
            "project:devin-vuln-automation",
            "phase:normalization",
            f"family:{slugify(raw_work_item['family_key'])}",
        ],
    }
    session = devin_request(
        "POST",
        f"/v3/organizations/{settings['devin_org_id']}/sessions",
        api_key=settings["devin_api_key"],
        payload=payload,
    )
    final_session = poll_session_until_terminal(
        settings,
        session["session_id"],
        settings["normalization_timeout_seconds"],
    )
    structured = final_session.get("structured_output") or {}
    if not structured:
        raise SystemExit(f"Normalization session {session['session_id']} completed without structured output")
    structured["normalization_session_id"] = session["session_id"]
    structured["normalization_session_url"] = session["url"]
    return structured


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


def count_active_remediation_sessions(settings: dict[str, Any]) -> int:
    count = 0
    for session in list_project_sessions(settings, phase="remediation"):
        if session.get("status") in {"new", "creating", "claimed", "running", "resuming"}:
            count += 1
    return count


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
        ],
    }
    session = devin_request(
        "POST",
        f"/v3/organizations/{settings['devin_org_id']}/sessions",
        api_key=settings["devin_api_key"],
        payload=payload,
    )
    github_request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue['number']}/comments",
        token=token,
        payload={
            "body": (
                "AWS remediation worker launched a Devin session.\n\n"
                f"- Session ID: `{session['session_id']}`\n"
                f"- Scope tier: `{work_item['scope_tier']}`\n"
                f"- Automation decision: `{work_item['automation_decision']}`\n"
                f"- Session URL: {session['url']}"
            )
        },
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
