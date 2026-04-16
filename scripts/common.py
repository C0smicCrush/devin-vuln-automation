from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
METRICS_DIR = ROOT / "metrics"
FIXTURES_DIR = ROOT / "fixtures"
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


def write_github_output(key: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


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


def build_issue_body(finding: dict[str, Any]) -> str:
    package_name = finding["package"]
    ecosystem = finding["ecosystem"]
    manifest = finding["manifest"]
    current_version = finding["current_version"]
    fixed_version = finding["fixed_version"]
    severity = finding["severity"]
    finding_id = finding["id"]
    description = finding["description"]

    return f"""## Summary
Resolve the `{package_name}` vulnerability identified by an upstream scanner signal consumed by the automation pipeline.

## Finding
- Finding ID: `{finding_id}`
- Ecosystem: `{ecosystem}`
- Severity: `{severity}`
- Current version: `{current_version}`
- Safe target version: `{fixed_version}`
- Affected manifest: `{manifest}`

## Context
{description}

## Source
- This issue is a tracked work item derived from scanner output.
- The automation repo owns orchestration; Devin owns the code change and PR.

## Acceptance Criteria
- Update `{package_name}` to a safe version with the smallest reasonable change.
- Run the relevant validation command for the touched dependency surface.
- Open a PR against `main` in this private Superset repo.
- Summarize risk, validation steps, and any blockers in the PR body.

## Devin Instructions
- Branch name: `devin/remediate/{slugify(package_name)}-{finding_id}`
- Prefer a minimal dependency bump over refactors.
- If the upgrade is blocked, explain the blocker and stop instead of forcing a breaking change.
"""


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
            "- This issue was normalized by the automation control plane before remediation.",
            "- Devin is expected to use the scoped test plan and stay within the identified surface area.",
        ]
    )
    return "\n".join(lines)


def build_devin_prompt(
    owner: str,
    repo: str,
    issue: dict[str, Any],
    repo_clone_url: str,
) -> str:
    issue_number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""
    return f"""You are handling a GitHub work item derived from a vulnerability finding in the repository `{owner}/{repo}`.

Repository to work in: {repo_clone_url}
Issue number: #{issue_number}
Issue title: {title}

Issue body:
{body}

Task requirements:
1. Inspect the vulnerable dependency or package context described in the issue.
2. Make the smallest safe dependency upgrade that addresses the finding.
3. Run the narrowest relevant validation command for the impacted area.
4. Open a pull request against the default branch of `{owner}/{repo}`.
5. Include a concise summary of the remediation, validation performed, and residual risk.

Output requirements:
- If you complete the work, ensure the PR is linked to the issue.
- If blocked, explain the blocker clearly and stop.
- Do not broaden scope beyond this finding.
"""


def build_remediation_prompt_from_work_item(
    owner: str,
    repo: str,
    issue: dict[str, Any],
    work_item: dict[str, Any],
    repo_clone_url: str,
) -> str:
    test_plan = work_item.get("test_plan", {})
    commands = test_plan.get("commands") or []
    manual_checks = test_plan.get("manual_checks") or []
    touched_files = test_plan.get("likely_touched_files") or []
    impacted_surface = test_plan.get("impacted_surface") or []
    commands_text = "\n".join(f"- `{item}`" for item in commands) or "- Use the narrowest credible validation available."
    manual_text = "\n".join(f"- {item}" for item in manual_checks) or "- None."
    files_text = "\n".join(f"- `{item}`" for item in touched_files) or "- Manifest and lockfile only unless required."
    surface_text = "\n".join(f"- {item}" for item in impacted_surface) or "- Scope not explicitly mapped."

    return f"""You are remediating a scoped work item in the repository `{owner}/{repo}`.

Repository to work in: {repo_clone_url}
Issue number: #{issue["number"]}
Issue title: {issue["title"]}

Normalized problem statement:
{work_item["problem_statement"]}

Scope tier:
- Tier: `{work_item["scope_tier"]}`
- Automation decision: `{work_item["automation_decision"]}`
- Confidence: `{work_item["confidence"]}`

Impacted surface:
{surface_text}

Likely touched files:
{files_text}

Required validation commands:
{commands_text}

Manual checks to mention if you need approval:
{manual_text}

Requirements:
1. Stay within the scoped surface area unless you discover a blocker that requires expansion.
2. Make the smallest safe fix that resolves the issue.
3. Run the required validation commands when possible and report exact results.
4. If the scope tier implies manual approval or the change becomes riskier than expected, stop and explain why.
5. Open a pull request against the default branch of `{owner}/{repo}` if the work can be completed safely.
"""


def build_normalization_prompt(work_item: dict[str, Any], test_matrix: dict[str, Any], repo_clone_url: str) -> str:
    matrix_text = json.dumps(test_matrix, indent=2, sort_keys=True)
    return f"""You are acting as a normalization and scoping engine for an event-driven remediation pipeline.

Repository under consideration: {repo_clone_url}

Raw work item:
{json.dumps(work_item, indent=2, sort_keys=True)}

Testing tier matrix:
{matrix_text}

Your job:
1. Convert the raw event into a concise problem statement.
2. Determine whether the work item is security or vulnerability related.
3. Assign the best fitting scope tier from the provided testing matrix.
4. Decide whether the remediation can be fully automated or requires manual approval.
5. Produce a concrete test plan with the narrowest credible validation commands.
6. Identify likely touched files and impacted surfaces.
7. Produce a canonical GitHub issue title/body if one should be created or updated.

Important constraints:
- Prefer conservative scoping over aggressive automation.
- If confidence is low, choose a more cautious tier and require manual approval.
- Keep the work item tightly bounded; do not invent broad refactors.
"""


def session_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {"type": "string"},
            "summary": {"type": "string"},
            "validation": {"type": "string"},
            "blocked_reason": {"type": "string"},
        },
        "required": ["result", "summary"],
    }


def normalization_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "problem_statement": {"type": "string"},
            "family_key": {"type": "string"},
            "is_security_related": {"type": "boolean"},
            "scope_tier": {"type": "string"},
            "automation_decision": {"type": "string"},
            "confidence": {"type": "string"},
            "summary": {"type": "string"},
            "canonical_issue_title": {"type": "string"},
            "canonical_issue_body": {"type": "string"},
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
            "problem_statement",
            "family_key",
            "is_security_related",
            "scope_tier",
            "automation_decision",
            "confidence",
            "summary",
            "canonical_issue_title",
            "canonical_issue_body",
            "issue_labels",
            "test_plan",
        ],
    }


def print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
