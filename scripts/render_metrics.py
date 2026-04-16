from __future__ import annotations

import argparse
from pathlib import Path

from common import ROOT, json_load, write_github_output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a markdown status summary from metrics JSON.")
    parser.add_argument("--metrics-file", default=str(ROOT / "metrics" / "latest.json"))
    parser.add_argument("--summary-file", default=str(ROOT / "metrics" / "summary.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    metrics = json_load(Path(args.metrics_file), default={})
    lines = [
        "## Devin Vulnerability Remediation Status",
        "",
        f"- Generated at: `{metrics.get('generated_at', 'n/a')}`",
        f"- Total sessions: `{metrics.get('total_sessions', 0)}`",
        f"- Active sessions: `{metrics.get('active_sessions', 0)}`",
        f"- Completed sessions: `{metrics.get('completed_sessions', 0)}`",
        f"- Blocked sessions: `{metrics.get('blocked_sessions', 0)}`",
        f"- Failed sessions: `{metrics.get('failed_sessions', 0)}`",
        f"- PRs opened: `{metrics.get('pull_requests_opened', 0)}`",
        "",
        "## Sessions",
        "",
    ]

    sessions = metrics.get("sessions", [])
    if not sessions:
        lines.append("- No sessions recorded yet.")
    else:
        for session in sessions:
            prs = session.get("pull_requests") or []
            first_pr = prs[0]["pr_url"] if prs else "n/a"
            lines.append(
                f"- Issue #{session['issue_number']} -> `{session['status']}` "
                f"(detail: `{session.get('status_detail') or 'n/a'}`, pr: {first_pr})"
            )

    summary_path = Path(args.summary_file)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_github_output("summary_file", str(summary_path))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
