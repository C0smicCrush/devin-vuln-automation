from __future__ import annotations

import unittest

from scripts.common import (
    build_remediation_prompt_from_work_item,
    canonical_issue_body_from_work_item,
    derive_family_key,
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

    def test_remediation_prompt_includes_validation_commands(self) -> None:
        issue = {"number": 67, "title": "Resolve DOMPurify vulnerability"}
        work_item = {
            "problem_statement": "DOMPurify advisory from npm audit",
            "scope_tier": "tier1_auto_targeted_runtime",
            "automation_decision": "auto",
            "confidence": "medium",
            "family_key": "dompurify",
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

    def test_family_key_prefers_finding_label(self) -> None:
        family = derive_family_key("Anything", ["finding:dompurify-001", "security-remediation"])
        self.assertEqual(family, "finding-dompurify-001")


if __name__ == "__main__":
    unittest.main()
