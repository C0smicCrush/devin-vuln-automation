from __future__ import annotations

import sys
import types
import unittest


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from lambda_poller import _build_issue_rollups, _extract_pr_number, _session_changed


class PollerTests(unittest.TestCase):
    def test_extract_pr_number_from_tags(self) -> None:
        self.assertEqual(_extract_pr_number(["issue:73", "pr:74"]), 74)
        self.assertIsNone(_extract_pr_number(["issue:73"]))

    def test_session_changed_false_when_state_is_same(self) -> None:
        session = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {"summary": "Still verifying", "verdict": "verified"},
        }
        self.assertFalse(_session_changed(session, dict(session)))

    def test_session_changed_true_when_verdict_changes(self) -> None:
        current = {
            "status": "exit",
            "status_detail": None,
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {"summary": "Verification complete", "verdict": "verified"},
        }
        previous = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {"summary": "Still verifying", "verdict": ""},
        }
        self.assertTrue(_session_changed(current, previous))

    def test_session_changed_ignores_provisional_structured_output_while_running(self) -> None:
        current = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {
                "summary": "In progress: reproducing the bug",
                "result": "manual_review",
                "questions_for_human": ["Should I proceed?"],
            },
        }
        previous = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {},
        }
        self.assertFalse(_session_changed(current, previous))

    def test_session_changed_true_when_questions_change(self) -> None:
        current = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {"summary": "Need info", "questions_for_human": ["Which dashboard should be restored?"]},
        }
        previous = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {"summary": "Need info", "questions_for_human": []},
        }
        self.assertTrue(_session_changed(current, previous))

    def test_session_changed_true_when_recommendation_changes(self) -> None:
        current = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {
                "summary": "Need a design decision",
                "questions_for_human": ["Which path should we take?"],
                "decision_options": ["Option A", "Option B"],
                "recommended_option": "Option B",
                "recommended_option_reason": "Smaller long-term risk.",
            },
        }
        previous = {
            "status": "running",
            "status_detail": "waiting_for_user",
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/74"}],
            "structured_output": {
                "summary": "Need a design decision",
                "questions_for_human": ["Which path should we take?"],
                "decision_options": ["Option A"],
                "recommended_option": "",
                "recommended_option_reason": "",
            },
        }
        self.assertTrue(_session_changed(current, previous))

    def test_issue_rollups_capture_first_pass_and_human_followups(self) -> None:
        sessions = [
            {
                "phase": "remediation",
                "issue_number": 69,
                "session_id": "rem-1",
                "status": "suspended",
                "status_detail": "waiting_for_user",
                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/70"}],
                "structured_output": {
                    "summary": "Need more information",
                    "questions_for_human": ["Which browser reproduced the XSS?"],
                },
                "tags": ["issue:69"],
            },
            {
                "phase": "remediation",
                "issue_number": 69,
                "session_id": "rem-2",
                "status": "exit",
                "status_detail": None,
                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/71"}],
                "structured_output": {"summary": "Follow-up remediation finished"},
                "tags": ["issue:69", "comment:9001", "trigger:comment_follow_up"],
            },
            {
                "phase": "verification",
                "issue_number": 69,
                "session_id": "ver-1",
                "status": "exit",
                "status_detail": None,
                "pull_requests": [],
                "structured_output": {"summary": "Verified", "verdict": "verified"},
                "tags": ["issue:69", "pr:71"],
            },
            {
                "phase": "remediation",
                "issue_number": 71,
                "session_id": "rem-3",
                "status": "exit",
                "status_detail": None,
                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/72"}],
                "structured_output": {"summary": "Single-pass remediation finished"},
                "tags": ["issue:71"],
            },
            {
                "phase": "verification",
                "issue_number": 71,
                "session_id": "ver-2",
                "status": "exit",
                "status_detail": None,
                "pull_requests": [],
                "structured_output": {"summary": "Verified first pass", "verdict": "verified"},
                "tags": ["issue:71", "pr:72"],
            },
        ]
        rollups = _build_issue_rollups(sessions)
        self.assertEqual(rollups["tracked_items_total"], 2)
        self.assertEqual(rollups["tracked_items_verified"], 2)
        self.assertEqual(rollups["tracked_items_verified_first_pass"], 1)
        self.assertEqual(rollups["tracked_items_needing_human_followup"], 1)
        self.assertEqual(rollups["tracked_items_with_multiple_remediation_loops"], 1)
        self.assertEqual(rollups["human_comment_followups_total"], 1)
        self.assertEqual(rollups["verification_sessions_total"], 2)
        self.assertEqual(rollups["verification_verdict_counts"]["verified"], 2)

    def test_issue_rollups_ignore_provisional_questions_while_running(self) -> None:
        sessions = [
            {
                "phase": "remediation",
                "issue_number": 84,
                "session_id": "rem-active",
                "status": "running",
                "status_detail": "working",
                "pull_requests": [],
                "structured_output": {
                    "summary": "In progress",
                    "questions_for_human": ["Should I proceed?"],
                    "blocked_reason": "Need confirmation",
                },
                "tags": ["issue:84"],
            }
        ]
        rollups = _build_issue_rollups(sessions)
        self.assertEqual(rollups["tracked_items_total"], 1)
        self.assertEqual(rollups["tracked_items_needing_human_followup"], 0)
        self.assertFalse(rollups["issue_rollups"][0]["human_info_requested"])


if __name__ == "__main__":
    unittest.main()
