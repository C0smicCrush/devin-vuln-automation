from __future__ import annotations

import argparse
from pathlib import Path

from common import FIXTURES_DIR, ROOT, json_dump, json_load, print_json, utc_now, write_github_output


def normalize_finding(raw: dict) -> dict:
    labels = ["security-remediation", "devin-candidate", f"severity:{raw['severity'].lower()}"]
    return {
        "id": raw["id"],
        "package": raw["package"],
        "ecosystem": raw["ecosystem"],
        "severity": raw["severity"].lower(),
        "current_version": raw["current_version"],
        "fixed_version": raw["fixed_version"],
        "manifest": raw["manifest"],
        "description": raw["description"],
        "labels": labels,
        "created_at": utc_now(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load deterministic scanner findings for the demo flow.")
    parser.add_argument(
        "--fixture",
        default=str(FIXTURES_DIR / "findings.sample.json"),
        help="Path to a JSON fixture containing findings.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "state" / "findings.json"),
        help="Destination JSON file for normalized findings.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    fixture_path = Path(args.fixture)
    output_path = Path(args.output)
    raw_payload = json_load(fixture_path, default={})
    findings = [normalize_finding(item) for item in raw_payload.get("findings", [])]
    payload = {
        "source": raw_payload.get("source", "fixture"),
        "generated_at": utc_now(),
        "count": len(findings),
        "findings": findings,
    }
    json_dump(output_path, payload)
    write_github_output("findings_count", str(len(findings)))
    print_json(payload)


if __name__ == "__main__":
    main()
