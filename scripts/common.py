from __future__ import annotations

import json
import os
import re
import sys
import time
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any
from urllib import error, parse, request

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def json_dump(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def json_load(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value in (None, ""):
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def utc_now() -> int:
    return int(time.time())


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def compact_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def http_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | list[Any] | None = None,
) -> Any:
    data = None
    merged_headers = {"Accept": "application/json"}
    if headers:
        merged_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        merged_headers["Content-Type"] = "application/json"
    req = request.Request(url, method=method.upper(), headers=merged_headers, data=data)
    try:
        with request.urlopen(req) as response:
            body = response.read().decode("utf-8")
            if not body:
                return {}
            return json.loads(body)
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method.upper()} {url} failed: {exc.code} {body}") from exc


def github_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    base = "https://api.github.com"
    url = f"{base}{path}"
    if query:
        url += "?" + parse.urlencode(query)
    return http_json(
        method,
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devin-vuln-automation",
        },
        payload=payload,
    )


def devin_request(
    method: str,
    path: str,
    api_key: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = f"https://api.devin.ai{path}"
    return http_json(
        method,
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "devin-vuln-automation",
        },
        payload=payload,
    )


def default_repo_config() -> tuple[str, str]:
    owner = env("TARGET_REPO_OWNER", "C0smicCrush")
    repo = env("TARGET_REPO_NAME", "superset-remediation")
    return owner, repo


def load_test_tier_matrix() -> dict[str, Any]:
    return json_load(CONFIG_DIR / "test_tiers.json", default={"tiers": {}})


@lru_cache(maxsize=1)
def load_prompt_templates() -> dict[str, str]:
    path = CONFIG_DIR / "prompts.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    templates = payload.get("prompts", payload)
    if not isinstance(templates, dict):
        raise SystemExit(f"Invalid prompt registry in {path}")
    return {str(key): str(value) for key, value in templates.items()}


def render_prompt(name: str, **context: Any) -> str:
    templates = load_prompt_templates()
    if name not in templates:
        raise SystemExit(f"Missing prompt template: {name}")
    return Template(templates[name]).safe_substitute(**context)


def is_security_related(title: str, body: str = "", labels: list[str] | None = None) -> bool:
    text = " ".join([title or "", body or "", " ".join(labels or [])]).lower()
    keywords = [
        "cve",
        "vulnerability",
        "vuln",
        "security",
        "xss",
        "csrf",
        "prototype pollution",
        "sanitiz",
        "dependency upgrade",
        "npm audit",
        "pip-audit",
        "trivy",
        "dependabot",
        "ghsa-",
    ]
    return any(keyword in text for keyword in keywords)


def derive_family_key(title: str, labels: list[str] | None = None) -> str:
    labels = labels or []
    for label in labels:
        if label.startswith("finding:"):
            return label.replace("finding:", "finding-")
    return slugify(title)[:64] or "generic-work-item"


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def seed_work_item_from_raw(raw_work_item: dict[str, Any], test_matrix: dict[str, Any]) -> dict[str, Any]:
    tiers = test_matrix.get("tiers") or {}
    title = raw_work_item.get("title") or "Remediation work item"
    body = raw_work_item.get("body") or ""
    labels = list(raw_work_item.get("labels") or [])
    source = raw_work_item.get("source") or {}
    text = " ".join([title, body, " ".join(labels)]).lower()
    if any(token in text for token in ["npm audit", "dependabot", "pip-audit", "ghsa-", "cve-", "package-lock", "requirements"]):
        tier = "tier0_auto_dependency_patch"
    else:
        tier = "tier1_auto_targeted_runtime"
    tier_defaults = tiers.get(tier, {})
    source_type = source.get("type", "unknown")

    problem_statement = _first_nonempty_line(body) or title
    if problem_statement != title:
        problem_statement = f"{title}: {problem_statement}"

    issue_labels = list(dict.fromkeys(labels + ["devin-remediate"]))
    if is_security_related(title, body, labels) and "security-remediation" not in issue_labels:
        issue_labels.append("security-remediation")
    if source_type == "manual_endpoint" and "manual-source" not in issue_labels:
        issue_labels.append("manual-source")

    likely_touched_files = []
    impacted_surface = []
    if tier == "tier0_auto_dependency_patch":
        likely_touched_files = ["package.json", "package-lock.json", "requirements.txt"]
        impacted_surface = ["Dependency manifests and lockfiles", "The runtime surface affected by the vulnerable dependency"]
    else:
        impacted_surface = ["Repository surface to be confirmed by the Devin remediation session"]

    work_item = {
        "event_type": raw_work_item.get("event_type"),
        "event_phase": "seeded",
        "source": source,
        "title": title,
        "body": body,
        "labels": labels,
        "created_at": raw_work_item.get("created_at"),
        "canonical_issue_number": raw_work_item.get("canonical_issue_number"),
        "family_key": raw_work_item.get("family_key") or derive_family_key(title, labels),
        "problem_statement": problem_statement,
        "summary": f"Seeded directly from `{source_type}` for a broad Devin remediation session.",
        "scope_tier": tier,
        "automation_decision": tier_defaults.get("automation_decision", "auto"),
        "confidence": "medium" if is_security_related(title, body, labels) else "low",
        "canonical_issue_title": title,
        "issue_labels": issue_labels,
        "test_plan": {
            "commands": list(tier_defaults.get("commands") or []),
            "manual_checks": list(tier_defaults.get("manual_checks") or []),
            "impacted_surface": impacted_surface,
            "likely_touched_files": likely_touched_files,
            "requires_new_tests": bool(tier_defaults.get("requires_new_tests", False)),
        },
    }
    work_item["canonical_issue_body"] = canonical_issue_body_from_work_item(work_item)
    return work_item


def canonical_issue_body_from_work_item(work_item: dict[str, Any]) -> str:
    test_plan = work_item.get("test_plan", {})
    touched_files = test_plan.get("likely_touched_files") or []
    impacted_surface = test_plan.get("impacted_surface") or []
    commands = test_plan.get("commands") or []
    manual_checks = test_plan.get("manual_checks") or []
    source = work_item.get("source", {})
    lines = [
        "## Problem Statement",
        work_item["problem_statement"],
        "",
        "## Source",
        f"- Source type: `{source.get('type', 'unknown')}`",
        f"- Source id: `{source.get('id', 'n/a')}`",
        f"- Source url: {source.get('url', 'n/a')}",
        "",
        "## Scope Tier",
        f"- Tier: `{work_item['scope_tier']}`",
        f"- Automation decision: `{work_item['automation_decision']}`",
        f"- Confidence: `{work_item['confidence']}`",
        "",
        "## Impacted Surface",
    ]
    if impacted_surface:
        lines.extend(f"- {item}" for item in impacted_surface)
    else:
        lines.append("- To be determined by remediation session.")
    lines.extend(["", "## Likely Touched Files"])
    if touched_files:
        lines.extend(f"- `{item}`" for item in touched_files)
    else:
        lines.append("- Lockfile and manifest updates expected.")
    lines.extend(["", "## Suggested Validation"])
    if commands:
        lines.extend(f"- `{item}`" for item in commands)
    else:
        lines.append("- Add commands during remediation.")
    if manual_checks:
        lines.extend(["", "## Manual Checks"])
        lines.extend(f"- {item}" for item in manual_checks)
    lines.extend(
        [
            "",
            "## Notes",
            "- This issue was shaped by the automation control plane to seed the Devin remediation loop.",
            "- Devin owns the engineering work: investigation, validation, code change, and PR creation.",
            "- The scoped test plan is guidance, but Devin should refine it if repository evidence requires a safer narrow adjustment.",
        ]
    )
    return "\n".join(lines)


def build_remediation_prompt_from_work_item(
    owner: str,
    repo: str,
    issue: dict[str, Any],
    work_item: dict[str, Any],
    repo_clone_url: str,
) -> str:
    test_matrix = load_test_tier_matrix()
    test_plan = work_item.get("test_plan", {})
    commands = test_plan.get("commands") or []
    manual_checks = test_plan.get("manual_checks") or []
    touched_files = test_plan.get("likely_touched_files") or []
    impacted_surface = test_plan.get("impacted_surface") or []
    source = work_item.get("source", {})
    commands_text = "\n".join(f"- `{item}`" for item in commands) or "- Use the narrowest credible validation available."
    manual_text = "\n".join(f"- {item}" for item in manual_checks) or "- None."
    files_text = "\n".join(f"- `{item}`" for item in touched_files) or "- Manifest and lockfile only unless required."
    surface_text = "\n".join(f"- {item}" for item in impacted_surface) or "- Scope not explicitly mapped."
    labels_text = "\n".join(f"- `{item}`" for item in (work_item.get("labels") or [])) or "- None."
    raw_body = work_item.get("body") or "- No raw event body provided."
    issue_body = issue.get("body") or "- No canonical issue body available."
    follow_up_comment_text = work_item.get("comment_body") or "- No follow-up comment context."
    reviewer_questions = "\n".join(f"- {item}" for item in (work_item.get("reviewer_questions") or [])) or "- None."
    reviewer_options = "\n".join(f"- {item}" for item in (work_item.get("reviewer_decision_options") or [])) or "- None."
    reviewer_recommendation = work_item.get("reviewer_recommended_option") or "- None."
    reviewer_recommendation_reason = work_item.get("reviewer_recommended_option_reason") or "- None."
    matrix_text = json.dumps(test_matrix, indent=2, sort_keys=True)

    return render_prompt(
        "remediation",
        owner=owner,
        repo=repo,
        repo_clone_url=repo_clone_url,
        issue_number=issue["number"],
        issue_title=issue["title"],
        source_type=source.get("type", "unknown"),
        source_action=source.get("action", "unknown"),
        source_id=source.get("id", "unknown"),
        source_url=source.get("url", "n/a"),
        labels_text=labels_text,
        raw_body=raw_body,
        issue_body=issue_body,
        problem_statement=work_item["problem_statement"],
        scope_tier=work_item["scope_tier"],
        automation_decision=work_item["automation_decision"],
        confidence=work_item["confidence"],
        surface_text=surface_text,
        files_text=files_text,
        commands_text=commands_text,
        manual_text=manual_text,
        follow_up_comment_text=follow_up_comment_text,
        reviewer_questions=reviewer_questions,
        reviewer_options=reviewer_options,
        reviewer_recommendation=reviewer_recommendation,
        reviewer_recommendation_reason=reviewer_recommendation_reason,
        matrix_text=matrix_text,
    )


def build_discovery_prompt(
    owner: str,
    repo: str,
    repo_clone_url: str,
    max_findings: int,
) -> str:
    return render_prompt(
        "discovery",
        owner=owner,
        repo=repo,
        repo_clone_url=repo_clone_url,
        max_findings=max_findings,
    )


def build_verification_prompt(
    owner: str,
    repo: str,
    issue: dict[str, Any],
    pr: dict[str, Any],
    remediation_output: dict[str, Any],
    repo_clone_url: str,
) -> str:
    remediation_text = json.dumps(remediation_output or {}, indent=2, sort_keys=True)
    issue_body = issue.get("body") or "- No issue body available."
    pr_body = pr.get("body") or "- No PR body available."
    return render_prompt(
        "verification",
        owner=owner,
        repo=repo,
        repo_clone_url=repo_clone_url,
        issue_number=issue["number"],
        issue_title=issue["title"],
        pr_number=pr["number"],
        pr_title=pr["title"],
        pr_url=pr["html_url"],
        issue_body=issue_body,
        pr_body=pr_body,
        remediation_text=remediation_text,
    )


def session_output_schema() -> dict[str, Any]:
    scanner_receipt = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {"type": "string"},
            "exit_code": {"type": "integer"},
            "advisories_reported": {
                "type": "array",
                "items": {"type": "string"},
            },
            "output_excerpt": {"type": "string"},
            "ran": {"type": "boolean"},
            "not_run_reason": {"type": "string"},
        },
        "required": ["command", "ran"],
    }
    test_receipt = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {"type": "string"},
            "exit_code": {"type": "integer"},
            "passed": {"type": "boolean"},
            "summary": {"type": "string"},
            "ran": {"type": "boolean"},
            "not_run_reason": {"type": "string"},
        },
        "required": ["command", "ran"],
    }
    test_plan = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "commands": {
                "type": "array",
                "items": {"type": "string"},
            },
            "manual_checks": {
                "type": "array",
                "items": {"type": "string"},
            },
            "impacted_surface": {
                "type": "array",
                "items": {"type": "string"},
            },
            "likely_touched_files": {
                "type": "array",
                "items": {"type": "string"},
            },
            "requires_new_tests": {"type": "boolean"},
        },
        "required": [
            "commands",
            "manual_checks",
            "impacted_surface",
            "likely_touched_files",
            "requires_new_tests",
        ],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {
                "type": "string",
                "enum": [
                    "ignored_non_actionable",
                    "manual_review",
                    "needs_human_input",
                    "completed",
                    "pr_opened",
                ],
            },
            "summary": {"type": "string"},
            "problem_statement": {"type": "string"},
            "family_key": {"type": "string"},
            "is_security_related": {"type": "boolean"},
            "scope_tier": {"type": "string"},
            "automation_decision": {"type": "string"},
            "confidence": {"type": "string"},
            "canonical_issue_title": {"type": "string"},
            "canonical_issue_body": {"type": "string"},
            "issue_labels": {
                "type": "array",
                "items": {"type": "string"},
            },
            "test_plan": test_plan,
            "blocked_reason": {"type": "string"},
            "pr_url": {"type": "string"},
            "scanner_before": scanner_receipt,
            "scanner_after": scanner_receipt,
            "tests": {
                "type": "array",
                "items": test_receipt,
            },
            "residual_risk": {"type": "string"},
            "questions_for_human": {
                "type": "array",
                "items": {"type": "string"},
            },
            "decision_options": {
                "type": "array",
                "items": {"type": "string"},
            },
            "recommended_option": {"type": "string"},
            "recommended_option_reason": {"type": "string"},
            "fixed_advisories": {
                "type": "array",
                "items": {"type": "string"},
            },
            "deferred_advisories": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["result", "summary"],
    }


def verification_output_schema() -> dict[str, Any]:
    test_receipt = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "command": {"type": "string"},
            "exit_code": {"type": "integer"},
            "passed": {"type": "boolean"},
            "summary": {"type": "string"},
            "ran": {"type": "boolean"},
            "not_run_reason": {"type": "string"},
        },
        "required": ["command", "ran"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string"},
            "summary": {"type": "string"},
            "confidence": {"type": "string"},
            "issue_fixed": {"type": "boolean"},
            "evidence_summary": {"type": "string"},
            "blocked_reason": {"type": "string"},
            "tests": {
                "type": "array",
                "items": test_receipt,
            },
            "questions_for_human": {
                "type": "array",
                "items": {"type": "string"},
            },
            "decision_options": {
                "type": "array",
                "items": {"type": "string"},
            },
            "recommended_option": {"type": "string"},
            "recommended_option_reason": {"type": "string"},
            "regressions_found": {
                "type": "array",
                "items": {"type": "string"},
            },
            "follow_up_actions": {
                "type": "array",
                "items": {"type": "string"},
            },
            "pr_url": {"type": "string"},
        },
        "required": ["verdict", "summary", "issue_fixed"],
    }


def discovery_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": "string"},
            "rejected_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["title", "reason"],
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string"},
                        "problem_statement": {"type": "string"},
                        "evidence": {"type": "string"},
                        "confidence": {"type": "string"},
                        "scope_tier": {"type": "string"},
                        "automation_decision": {"type": "string"},
                        "issue_labels": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "test_plan": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "commands": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "manual_checks": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "impacted_surface": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "likely_touched_files": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "requires_new_tests": {"type": "boolean"},
                            },
                            "required": [
                                "commands",
                                "manual_checks",
                                "impacted_surface",
                                "likely_touched_files",
                                "requires_new_tests",
                            ],
                        },
                    },
                    "required": [
                        "id",
                        "title",
                        "problem_statement",
                        "evidence",
                        "confidence",
                        "scope_tier",
                        "automation_decision",
                        "issue_labels",
                        "test_plan",
                    ],
                },
            },
        },
        "required": ["summary", "findings"],
    }


def print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
