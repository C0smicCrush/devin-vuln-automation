from __future__ import annotations

import unittest

from scripts.common import (
    build_discovery_prompt,
    build_remediation_prompt_from_work_item,
    build_verification_prompt,
    canonical_issue_body_from_work_item,
    derive_family_key,
    discovery_output_schema,
    load_test_tier_matrix,
    seed_work_item_from_raw,
    session_output_schema,
    verification_output_schema,
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
            "reviewer_questions": ["Do you want the fix limited to the package bump?"],
            "reviewer_decision_options": ["Option A: package bump only", "Option B: package bump plus targeted regression test"],
            "reviewer_recommended_option": "Option B: package bump plus targeted regression test",
            "reviewer_recommended_option_reason": "It gives a better long-term guardrail with limited extra scope.",
            "comment_body": "Please proceed with Option B.",
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
        self.assertIn("First decide whether this input is actionable", prompt)
        self.assertIn("scanner_before", prompt)
        self.assertIn("scanner_after", prompt)
        self.assertIn("one bounded PR per advisory or CVE", prompt)
        self.assertIn("Option B: package bump plus targeted regression test", prompt)
        self.assertIn("Please proceed with Option B.", prompt)

    def test_family_key_prefers_finding_label(self) -> None:
        family = derive_family_key("Anything", ["finding:dompurify-001", "security-remediation"])
        self.assertEqual(family, "finding-dompurify-001")

    def test_session_schema_includes_validation_receipts(self) -> None:
        schema = session_output_schema()
        props = schema["properties"]
        for key in ("scanner_before", "scanner_after", "tests", "pr_url", "residual_risk"):
            self.assertIn(key, props)
        self.assertEqual(schema["properties"]["tests"]["type"], "array")
        self.assertIn("questions_for_human", props)
        self.assertIn("problem_statement", props)
        self.assertIn("automation_decision", props)
        self.assertIn("test_plan", props)
        self.assertEqual(set(props["result"]["enum"]), {"ignored_non_actionable", "manual_review", "needs_human_input", "completed", "pr_opened"})

    def test_remediation_prompt_describes_single_session_gating(self) -> None:
        issue = {
            "number": 73,
            "title": "Tracked issue",
            "body": "Body",
        }
        work_item = {
            "problem_statement": "Follow-up request",
            "scope_tier": "tier1_auto_targeted_runtime",
            "automation_decision": "auto",
            "confidence": "medium",
            "family_key": "tracked-issue",
            "source": {"type": "github_pr_comment", "id": "9001", "action": "created", "url": "https://example.test/comment/9001"},
            "body": "Please keep the scope narrow.",
            "labels": ["devin-remediate"],
            "test_plan": {
                "commands": ["npm run test -- --runInBand"],
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
        self.assertIn("single end-to-end remediation operator", prompt)
        self.assertIn("ignored_non_actionable", prompt)
        self.assertIn("needs_human_input", prompt)
        self.assertIn("Testing tier matrix", prompt)
        self.assertIn("normal non-draft pull request", prompt)
        self.assertIn("do not open a draft PR", prompt)
        self.assertIn("Bring up the actual repository and relevant product or runtime surface every time you run", prompt)
        self.assertIn("bring up the actual app, product surface, or runtime path as part of validation", prompt)
        self.assertIn("you must reproduce through the repository's real local runtime using Docker Compose", prompt)
        self.assertIn("Do not ask the human to choose between reproduction paths", prompt)
        self.assertIn("Jest-only, unit-test-only, or static-analysis-only reproduction is not sufficient", prompt)
        self.assertIn("include the exact Docker Compose commands", prompt)
        self.assertIn("reproduce the reported behavior first", prompt)
        self.assertIn("re-run the same reproduction after the fix", prompt)
        self.assertIn("treat that follow-up comment as the latest controlling instruction", prompt)
        self.assertIn("A human follow-up comment can override a prior `manual_review`", prompt)
        self.assertIn("Do not re-ask questions that the latest human follow-up comment already answered", prompt)

    def test_verification_prompt_is_strict_and_independent(self) -> None:
        issue = {
            "number": 73,
            "title": "Soft delete SIP",
            "body": "Please validate that the PR really fixes the issue.",
        }
        pr = {
            "number": 74,
            "title": "feat(models): add SoftDeleteMixin skeleton",
            "html_url": "https://github.com/C0smicCrush/superset-remediation/pull/74",
            "body": "Draft PR body",
        }
        remediation_output = {
            "summary": "Opened a PR and claimed the issue is fixed.",
            "scanner_before": {"command": "npm audit --json", "ran": True},
        }
        prompt = build_verification_prompt(
            "C0smicCrush",
            "superset-remediation",
            issue,
            pr,
            remediation_output,
            "https://github.com/C0smicCrush/superset-remediation",
        )
        self.assertIn("strict post-PR verification reviewer", prompt)
        self.assertIn("Do not trust the PR description", prompt)
        self.assertIn("Think like a senior engineer", prompt)
        self.assertIn("bring up the relevant product surface", prompt)
        self.assertIn("verdict", prompt)
        self.assertIn("questions_for_human", prompt)
        self.assertIn("decision_options", prompt)
        self.assertIn("recommended_option", prompt)

    def test_verification_schema_captures_verdict_and_checks(self) -> None:
        schema = verification_output_schema()
        props = schema["properties"]
        for key in ("verdict", "summary", "issue_fixed", "tests", "evidence_summary", "pr_url", "questions_for_human", "decision_options", "recommended_option", "recommended_option_reason"):
            self.assertIn(key, props)
        self.assertEqual(props["tests"]["type"], "array")
        self.assertIn("verdict", schema["required"])
        self.assertIn("issue_fixed", schema["required"])

    def test_discovery_prompt_requires_rejected_findings(self) -> None:
        prompt = build_discovery_prompt(
            "C0smicCrush",
            "superset-remediation",
            "https://github.com/C0smicCrush/superset-remediation",
            1,
        )
        self.assertIn("rejected_findings", prompt)
        self.assertIn("one advisory or CVE per finding", prompt)

    def test_discovery_schema_supports_rejected_findings(self) -> None:
        schema = discovery_output_schema()
        props = schema["properties"]
        self.assertIn("rejected_findings", props)
        self.assertEqual(props["rejected_findings"]["type"], "array")
        required_item_keys = schema["properties"]["rejected_findings"]["items"]["required"]
        self.assertIn("title", required_item_keys)
        self.assertIn("reason", required_item_keys)

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
