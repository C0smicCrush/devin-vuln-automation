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
Remediate the `{package_name}` vulnerability identified by the automation pipeline.

## Finding
- Finding ID: `{finding_id}`
- Ecosystem: `{ecosystem}`
- Severity: `{severity}`
- Current version: `{current_version}`
- Safe target version: `{fixed_version}`
- Affected manifest: `{manifest}`

## Context
{description}

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


def build_devin_prompt(
    owner: str,
    repo: str,
    issue: dict[str, Any],
    repo_clone_url: str,
) -> str:
    issue_number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""
    return f"""You are remediating a vulnerability issue in the repository `{owner}/{repo}`.

Repository to work in: {repo_clone_url}
Issue number: #{issue_number}
Issue title: {title}

Issue body:
{body}

Task requirements:
1. Reproduce or inspect the vulnerable dependency described in the issue.
2. Make the smallest safe dependency upgrade that addresses the finding.
3. Run the narrowest relevant validation command for the impacted area.
4. Open a pull request against the default branch of `{owner}/{repo}`.
5. Include a concise summary of the remediation, validation performed, and residual risk.

Output requirements:
- If you complete the work, ensure the PR is linked to the issue.
- If blocked, explain the blocker clearly and stop.
- Do not broaden scope beyond this finding.
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


def print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
