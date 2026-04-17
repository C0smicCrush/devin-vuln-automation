from __future__ import annotations

import unittest

from scripts.common import (
    build_remediation_prompt_from_work_item,
    canonical_issue_body_from_work_item,
    derive_family_key,
    load_test_tier_matrix,
    seed_work_item_from_raw,
    should_run_preflight,
)


class CommonPromptTests(unittest.TestCase):
    def test_canonical_issue_body_contains_scope_and_commands(self) -> None:
        work_item = {
            "problem_statement": "Resolve DOMPurify vulnerability from npm audit output.",
            "scope_tier": "tier1_auto_targeted_runtime",
            "automation_decision": "auto",
            "confidence": "medium",
            "source": {"type": "manual_endpoint", "id": "abc", "url": "https://example.test"},
            "test_plan": {
                "commands": ["npm run test -- --runInBand", "npm run build"],
                "manual_checks": ["Review sanitization paths."],
                "impacted_surface": ["Frontend runtime behavior"],
                "likely_touched_files": ["package.json", "package-lock.json"],
                "requires_new_tests": True,
            },
        }
        body = canonical_issue_body_from_work_item(work_item)
        self.assertIn("## Scope Tier", body)
        self.assertIn("tier1_auto_targeted_runtime", body)
        self.assertIn("npm run build", body)
        self.assertIn("Devin owns the engineering work", body)

    def test_remediation_prompt_includes_validation_commands(self) -> None:
        issue = {
            "number": 67,
            "title": "Resolve DOMPurify vulnerability",
            "body": "Tracked work item for DOMPurify remediation.",
        }
        work_item = {
            "problem_statement": "DOMPurify advisory from npm audit",
            "scope_tier": "tier1_auto_targeted_runtime",
            "automation_decision": "auto",
            "confidence": "medium",
            "family_key": "dompurify",
            "source": {"type": "manual_endpoint", "id": "manual-1", "action": "submitted", "url": "https://example.test"},
            "body": "Upstream audit says DOMPurify needs a safe upgrade.",
            "labels": ["security-remediation"],
            "test_plan": {
                "commands": ["npm run test -- --runInBand", "npm run build"],
                "manual_checks": [],
                "impacted_surface": ["Frontend runtime behavior"],
                "likely_touched_files": ["package.json"],
                "requires_new_tests": True,
            },
        }
        prompt = build_remediation_prompt_from_work_item(
            "C0smicCrush",
            "superset-remediation",
            issue,
            work_item,
            "https://github.com/C0smicCrush/superset-remediation",
        )
        self.assertIn("npm run test -- --runInBand", prompt)
        self.assertIn("Frontend runtime behavior", prompt)
        self.assertIn("end-to-end remediation operator", prompt)
        self.assertIn("Investigate whether the issue is actionable", prompt)

    def test_family_key_prefers_finding_label(self) -> None:
        family = derive_family_key("Anything", ["finding:dompurify-001", "security-remediation"])
        self.assertEqual(family, "finding-dompurify-001")

    def test_security_github_issue_skips_preflight(self) -> None:
        raw = {
            "event_type": "github_issue",
            "source": {"type": "github_issue", "id": "1", "action": "opened", "url": "https://example.test/1"},
            "title": "Resolve npm audit DOMPurify vulnerability",
            "body": "Security remediation for GHSA advisory.",
            "labels": ["security-remediation", "devin-remediate"],
        }
        self.assertFalse(should_run_preflight(raw))

    def test_linear_ticket_uses_preflight(self) -> None:
        raw = {
            "event_type": "linear_ticket",
            "source": {"type": "linear_ticket", "id": "LIN-1", "action": "created", "url": "https://linear.test/LIN-1"},
            "title": "Investigate frontend package risk",
            "body": "May require remediation.",
            "labels": [],
        }
        self.assertTrue(should_run_preflight(raw))

    def test_discovery_event_uses_preflight(self) -> None:
        raw = {
            "event_type": "scheduled_discovery",
            "source": {"type": "manual_endpoint", "id": "disc-1", "action": "submitted", "url": "https://example.test/discovery"},
            "title": "Daily repo discovery run",
            "body": "Inspect the repo for actionable security findings.",
            "labels": [],
        }
        self.assertTrue(should_run_preflight(raw))

    def test_seed_work_item_from_raw_uses_tier_defaults(self) -> None:
        raw = {
            "source": {"type": "manual_endpoint", "id": "manual-1", "action": "submitted", "url": "https://example.test"},
            "title": "Resolve npm audit DOMPurify vulnerability",
            "body": "Upgrade the vulnerable dependency with minimal change.",
            "labels": ["security-remediation"],
            "family_key": "dompurify",
        }
        work_item = seed_work_item_from_raw(raw, load_test_tier_matrix())
        self.assertEqual(work_item["scope_tier"], "tier0_auto_dependency_patch")
        self.assertEqual(work_item["automation_decision"], "auto")
        self.assertIn("npm audit --package-lock-only", work_item["test_plan"]["commands"])
        self.assertIn("manual-source", work_item["issue_labels"])


if __name__ == "__main__":
    unittest.main()
