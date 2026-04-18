from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import parse_incoming_event


class EventParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {
            "github_webhook_secret": "",
            "linear_webhook_secret": "",
            "gh_token": "token",
            "owner": "C0smicCrush",
            "repo": "superset-remediation",
            "metrics_bucket": "",
        }

    def test_manual_event_maps_to_raw_envelope(self) -> None:
        event = {
            "rawPath": "/manual",
            "headers": {},
            "body": json.dumps(
                {
                    "id": "manual-1",
                    "title": "Security work item",
                    "body": "Investigate DOMPurify vulnerability",
                    "labels": ["security-remediation"],
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_phase"], "raw")
        self.assertEqual(parsed["event_type"], "manual")
        self.assertEqual(parsed["source"]["type"], "manual_endpoint")
        self.assertEqual(parsed["title"], "Security work item")

    def test_linear_event_maps_to_stub_source(self) -> None:
        event = {
            "rawPath": "/linear",
            "headers": {},
            "body": json.dumps(
                {
                    "id": "lin-1",
                    "title": "Linear ticket",
                    "description": "Security task from Linear",
                    "labels": ["security-remediation"],
                    "url": "https://linear.app/example/SEC-1",
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_type"], "linear_ticket")
        self.assertEqual(parsed["source"]["type"], "linear_ticket")
        self.assertEqual(parsed["source"]["id"], "lin-1")

    def test_manual_event_preserves_explicit_discovery_type(self) -> None:
        event = {
            "rawPath": "/manual",
            "headers": {},
            "body": json.dumps(
                {
                    "id": "manual-discovery",
                    "event_type": "scheduled_discovery",
                    "title": "Daily discovery",
                    "body": "Inspect for actionable findings.",
                    "labels": [],
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_type"], "scheduled_discovery")

    def test_github_non_issue_event_is_ignored(self) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "push"},
            "body": json.dumps({}),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertTrue(parsed["ignored"])

    def test_github_issue_without_devin_label_is_ignored(self) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issues"},
            "body": json.dumps(
                {
                    "action": "opened",
                    "issue": {
                        "id": 1,
                        "number": 1,
                        "title": "Investigate frontend issue",
                        "body": "Potential problem",
                        "labels": [{"name": "security"}],
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertTrue(parsed["ignored"])

    def test_github_non_devin_label_event_is_ignored(self) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issues"},
            "body": json.dumps(
                {
                    "action": "labeled",
                    "label": {"name": "security"},
                    "issue": {
                        "id": 2,
                        "number": 2,
                        "title": "Resolve vulnerability",
                        "body": "Tracked issue",
                        "labels": [{"name": "security"}, {"name": "devin-remediate"}],
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertTrue(parsed["ignored"])

    @patch("aws_runtime.register_comment_event_once", return_value=True)
    def test_github_issue_comment_on_tracked_issue_maps_to_follow_up(self, _dedupe) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issue_comment"},
            "body": json.dumps(
                {
                    "action": "created",
                    "issue": {
                        "id": 73,
                        "number": 73,
                        "title": "Tracked issue",
                        "body": "Original issue body",
                        "html_url": "https://github.com/example/repo/issues/73",
                        "labels": [{"name": "devin-remediate"}],
                    },
                    "comment": {
                        "id": 9001,
                        "body": "Here is the missing information you asked for.",
                        "html_url": "https://github.com/example/repo/issues/73#issuecomment-1",
                        "created_at": "2026-04-18T00:00:00Z",
                        "user": {"login": "aatmodhee", "type": "User"},
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_type"], "github_issue_comment")
        self.assertEqual(parsed["source"]["type"], "github_issue_comment")
        self.assertEqual(parsed["canonical_issue_number"], 73)
        self.assertEqual(parsed["comment_id"], "9001")
        self.assertEqual(parsed["follow_up_reason"], "requested_info")

    @patch("aws_runtime.register_comment_event_once", return_value=False)
    def test_duplicate_issue_comment_event_is_ignored(self, _dedupe) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issue_comment"},
            "body": json.dumps(
                {
                    "action": "created",
                    "issue": {
                        "id": 73,
                        "number": 73,
                        "title": "Tracked issue",
                        "body": "Original issue body",
                        "html_url": "https://github.com/example/repo/issues/73",
                        "labels": [{"name": "devin-remediate"}],
                    },
                    "comment": {
                        "id": 9004,
                        "body": "Same webhook delivered twice.",
                        "html_url": "https://github.com/example/repo/issues/73#issuecomment-4",
                        "created_at": "2026-04-18T00:00:00Z",
                        "user": {"login": "aatmodhee", "type": "User"},
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertTrue(parsed["ignored"])

    @patch("aws_runtime.github_request")
    @patch("aws_runtime._resolve_canonical_issue_for_pr", return_value=73)
    @patch("aws_runtime.register_comment_event_once", return_value=True)
    def test_issue_comment_on_pr_maps_back_to_canonical_issue(self, _dedupe, _resolve, mock_github_request) -> None:
        mock_github_request.return_value = {
            "id": 73,
            "number": 73,
            "title": "Tracked issue",
            "body": "Canonical issue body",
            "html_url": "https://github.com/example/repo/issues/73",
            "labels": [{"name": "devin-remediate"}],
        }
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issue_comment"},
            "body": json.dumps(
                {
                    "action": "created",
                    "issue": {
                        "id": 74,
                        "number": 74,
                        "title": "PR title",
                        "body": "PR body",
                        "html_url": "https://github.com/example/repo/pull/74",
                        "pull_request": {"url": "https://api.github.com/repos/example/repo/pulls/74"},
                        "labels": [],
                    },
                    "comment": {
                        "id": 9005,
                        "body": "Can you validate this against the actual user workflow?",
                        "html_url": "https://github.com/example/repo/pull/74#issuecomment-5",
                        "created_at": "2026-04-18T00:00:00Z",
                        "user": {"login": "aatmodhee", "type": "User"},
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_type"], "github_pr_comment")
        self.assertEqual(parsed["canonical_issue_number"], 73)
        self.assertEqual(parsed["parent_pr_number"], 74)

    @patch("aws_runtime.github_request")
    @patch("aws_runtime._resolve_canonical_issue_for_pr", return_value=73)
    @patch("aws_runtime.register_comment_event_once", return_value=True)
    def test_github_pr_comment_maps_back_to_canonical_issue(self, _dedupe, _resolve, mock_github_request) -> None:
        mock_github_request.return_value = {
            "id": 73,
            "number": 73,
            "title": "Tracked issue",
            "body": "Canonical issue body",
            "html_url": "https://github.com/example/repo/issues/73",
            "labels": [{"name": "devin-remediate"}],
        }
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "pull_request_review_comment"},
            "body": json.dumps(
                {
                    "action": "created",
                    "pull_request": {"number": 74},
                    "comment": {
                        "id": 9002,
                        "body": "Please validate this against the original bug report too.",
                        "html_url": "https://github.com/example/repo/pull/74#discussion_r1",
                        "created_at": "2026-04-18T00:00:00Z",
                        "user": {"login": "aatmodhee", "type": "User"},
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertEqual(parsed["event_type"], "github_pr_comment")
        self.assertEqual(parsed["source"]["type"], "github_pr_comment")
        self.assertEqual(parsed["canonical_issue_number"], 73)
        self.assertEqual(parsed["parent_pr_number"], 74)

    def test_github_issue_comment_from_automation_is_ignored(self) -> None:
        event = {
            "rawPath": "/github",
            "headers": {"x-github-event": "issue_comment"},
            "body": json.dumps(
                {
                    "action": "created",
                    "issue": {
                        "id": 73,
                        "number": 73,
                        "title": "Tracked issue",
                        "body": "Original issue body",
                        "html_url": "https://github.com/example/repo/issues/73",
                        "labels": [{"name": "devin-remediate"}],
                    },
                    "comment": {
                        "id": 9003,
                        "body": "AWS poller status update.\n\n- Session ID: `abc`",
                        "html_url": "https://github.com/example/repo/issues/73#issuecomment-2",
                        "created_at": "2026-04-18T00:00:00Z",
                        "user": {"login": "C0smicCrush", "type": "User"},
                    },
                }
            ),
        }
        parsed = parse_incoming_event(event, self.settings)
        self.assertTrue(parsed["ignored"])


if __name__ == "__main__":
    unittest.main()
