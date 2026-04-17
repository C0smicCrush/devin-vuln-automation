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


def _first_nonempty_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def should_run_preflight(raw_work_item: dict[str, Any]) -> bool:
    source = raw_work_item.get("source", {})
    event_type = str(raw_work_item.get("event_type", "")).lower()
    if source.get("type") == "linear_ticket":
        return True
    return event_type in {
        "scheduled_discovery",
        "devin_discovery",
        "discovery",
        "repo_review",
    }


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
            "- This issue was shaped by the automation control plane to seed the Devin remediation loop.",
            "- Devin owns the engineering work: investigation, validation, code change, and PR creation.",
            "- The scoped test plan is guidance, but Devin should refine it if repository evidence requires a safer narrow adjustment.",
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
    source = work_item.get("source", {})
    commands_text = "\n".join(f"- `{item}`" for item in commands) or "- Use the narrowest credible validation available."
    manual_text = "\n".join(f"- {item}" for item in manual_checks) or "- None."
    files_text = "\n".join(f"- `{item}`" for item in touched_files) or "- Manifest and lockfile only unless required."
    surface_text = "\n".join(f"- {item}" for item in impacted_surface) or "- Scope not explicitly mapped."
    labels_text = "\n".join(f"- `{item}`" for item in (work_item.get("labels") or [])) or "- None."
    raw_body = work_item.get("body") or "- No raw event body provided."
    issue_body = issue.get("body") or "- No canonical issue body available."

    return f"""You are the end-to-end remediation operator for a scoped engineering work item in `{owner}/{repo}`.

Repository to work in: {repo_clone_url}
Issue number: #{issue["number"]}
Issue title: {issue["title"]}

Source event:
- Type: `{source.get('type', 'unknown')}`
- Action: `{source.get('action', 'unknown')}`
- Source id: `{source.get('id', 'unknown')}`
- Source url: {source.get('url', 'n/a')}

Source labels:
{labels_text}

Raw event body:
{raw_body}

Canonical issue body:
{issue_body}

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
1. Investigate whether the issue is actionable in this repository before making changes.
2. Treat the provided scope and test plan as preflight guidance, but refine them if repository evidence supports a safer narrow adjustment.
3. Stay within the smallest safe surface area unless you discover a blocker that requires explicit expansion.
4. Make the smallest safe fix that resolves the issue.
5. For dependency or scanner-driven work, prefer one bounded PR per advisory or CVE. If the issue aggregates multiple unrelated advisories, fix only the tightest subset in this PR and explain the remaining ones in the PR body so they can be tracked separately.
6. If the issue is not actionable, if the scope tier implies manual approval, or if the change becomes riskier than expected, stop and explain why.
7. Open a pull request against the default branch of `{owner}/{repo}` if the work can be completed safely.

Validation contract (must be produced):
A. Before making any code change, capture a "scanner_before" receipt by running the most relevant scanner for this work item inside your sandbox. Examples by ecosystem:
   - npm: `npm audit --json` (or `npm audit`) for the affected package.
   - python: `pip-audit -r <requirements-file> --no-deps` or `pip-audit --strict`.
   - generic/security: the narrowest credible reproduction or inspection command.
   Record the exact command, exit code, and the advisory IDs it reports.
B. After making the fix, re-run the exact same scanner and capture a "scanner_after" receipt. The fix is only considered validated if the targeted advisory is no longer reported.
C. Run the scoped tests listed above (or the narrowest credible substitute if those tests are not appropriate for this repository state) and record exact commands, exit codes, and a short pass/fail summary.
D. If any required validation command cannot run in the sandbox (missing toolchain, network-locked, etc.), state exactly which command failed and why, and do not pretend the fix is validated.
E. The PR description must include both receipts (before/after scanner output plus test outcomes) so a reviewer can verify the fix without re-running anything.

Output expectations:
- You own the engineering loop for this work item: investigation, fix selection, validation, and PR/reporting.
- Populate the structured output fields `scanner_before`, `scanner_after`, `tests`, `residual_risk`, and `pr_url` honestly. Empty or fabricated receipts are a failure mode.
- If you stop, explain the blocker or manual-review reason clearly in `blocked_reason`.
- Do not broaden scope into unrelated refactors.
"""


def build_normalization_prompt(work_item: dict[str, Any], test_matrix: dict[str, Any], repo_clone_url: str) -> str:
    matrix_text = json.dumps(test_matrix, indent=2, sort_keys=True)
    return f"""You are performing preflight scoping for an event-driven Devin remediation pipeline.

Repository under consideration: {repo_clone_url}

Raw work item:
{json.dumps(work_item, indent=2, sort_keys=True)}

Testing tier matrix:
{matrix_text}

Your job:
1. Convert the raw event into an initial problem statement for a downstream remediation session.
2. Determine whether the work item is security or vulnerability related.
3. Assign the best fitting initial scope tier from the provided testing matrix.
4. Decide whether the remediation is a good candidate for autonomous execution or should default to manual approval.
5. Produce an initial test plan with the narrowest credible validation commands.
6. Identify likely touched files and impacted surfaces.
7. Produce a canonical GitHub issue title/body if one should be created or updated.

Important constraints:
- Prefer conservative scoping over aggressive automation.
- If confidence is low, choose a more cautious tier and require manual approval.
- Keep the work item tightly bounded; do not invent broad refactors.
- You are producing preflight guidance, not the final engineering decision. The downstream Devin remediation session will re-evaluate repository reality before acting.
"""


def build_discovery_prompt(
    owner: str,
    repo: str,
    repo_clone_url: str,
    max_findings: int,
) -> str:
    return f"""You are performing a bounded discovery review for `{owner}/{repo}`.

Repository to inspect: {repo_clone_url}

Goal:
- Find at most {max_findings} actionable security or vulnerability remediation candidates.

Requirements:
1. Only report findings that are strongly supported by repository evidence or deterministic dependency/security evidence (scanner output, pinned-vulnerable versions, advisory IDs, etc.).
2. Prefer concrete dependency vulnerabilities, unsafe configuration, or clearly actionable security flaws over speculative concerns.
3. Validate that each accepted finding is real enough to justify creating a tracked GitHub issue in this repository.
4. Prefer one advisory or CVE per finding so downstream remediation PRs stay bounded. Only aggregate multiple advisories into one finding if they share a single package bump with no other surface.
5. If you are not confident a finding is real and actionable, do not include it in `findings`. Instead, record it in `rejected_findings` with a short, concrete reason.
6. Keep the accepted list short and high signal. Returning zero accepted findings is acceptable.

For each accepted finding you include:
- Provide a concise title and problem statement.
- State the evidence and why it is actionable in this repository.
- Suggest the smallest safe remediation scope.
- Choose an initial scope tier and automation decision.
- Provide a narrow validation plan.
- Include labels that would make sense on a tracked GitHub issue.

For each rejected finding you considered but discarded:
- Record the advisory ID or short title.
- Explain in one or two sentences why it is not actionable here (false positive, unused code path, upstream-only advisory, too-large bump, already fixed, etc.).
- This audit trail is part of the deliverable; an empty `rejected_findings` list is acceptable only if nothing was considered and rejected.

Important constraints:
- Do not propose broad refactors.
- Do not include hypothetical or weakly supported issues in `findings`.
- Prefer fewer, higher-confidence findings over many marginal ones.
"""


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
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "result": {"type": "string"},
            "summary": {"type": "string"},
            "validation": {"type": "string"},
            "blocked_reason": {"type": "string"},
            "pr_url": {"type": "string"},
            "scanner_before": scanner_receipt,
            "scanner_after": scanner_receipt,
            "tests": {
                "type": "array",
                "items": test_receipt,
            },
            "residual_risk": {"type": "string"},
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
