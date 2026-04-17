from __future__ import annotations

import json
import sys
import types
import unittest


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import parse_incoming_event


class EventParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = {
            "github_webhook_secret": "",
            "linear_webhook_secret": "",
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


if __name__ == "__main__":
    unittest.main()
