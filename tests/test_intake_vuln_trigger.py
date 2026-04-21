from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from lambda_intake import handler as intake_handler


class VulnTriggerIntakeTests(unittest.TestCase):
    def test_vuln_trigger_short_circuits_to_discovery(self) -> None:
        event = {
            "rawPath": "/vuln-trigger",
            "headers": {},
            "body": "",
        }
        expected = {
            "action": "completed",
            "findings_count": 0,
            "issues_opened_by_devin": [],
            "issues_skipped_as_duplicate": [],
            "issue_creation_failures": [],
        }
        with patch("lambda_discovery.handler", return_value=expected) as mock_handler, \
                patch("lambda_intake.load_runtime_settings") as mock_settings, \
                patch("lambda_intake.parse_incoming_event") as mock_parse, \
                patch("lambda_intake.enqueue_work_item") as mock_enqueue:
            response = intake_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["body"], expected)
        mock_handler.assert_called_once_with({}, None)
        mock_settings.assert_not_called()
        mock_parse.assert_not_called()
        mock_enqueue.assert_not_called()

    def test_vuln_trigger_forwards_max_findings_from_body(self) -> None:
        event = {
            "rawPath": "/vuln-trigger",
            "headers": {},
            "body": json.dumps({"max_findings": 3}),
        }
        with patch("lambda_discovery.handler", return_value={"action": "completed"}) as mock_handler:
            intake_handler(event, None)
        mock_handler.assert_called_once_with({"max_findings": 3}, None)

    def test_vuln_trigger_ignores_invalid_body(self) -> None:
        event = {
            "rawPath": "/vuln-trigger",
            "headers": {},
            "body": "not-json",
        }
        with patch("lambda_discovery.handler", return_value={"action": "completed"}) as mock_handler:
            intake_handler(event, None)
        mock_handler.assert_called_once_with({}, None)

    def test_non_vuln_trigger_path_still_uses_normal_intake(self) -> None:
        event = {
            "rawPath": "/manual",
            "headers": {},
            "body": json.dumps({"title": "t", "body": "b"}),
        }
        with patch("lambda_intake.load_runtime_settings", return_value={}) as mock_settings, \
                patch(
                    "lambda_intake.parse_incoming_event",
                    return_value={
                        "event_phase": "raw",
                        "source": {"type": "manual_endpoint"},
                    },
                ) as mock_parse, \
                patch(
                    "lambda_intake.enqueue_work_item",
                    return_value={"message_id": "m-1"},
                ) as mock_enqueue, \
                patch("lambda_discovery.handler") as mock_discovery:
            response = intake_handler(event, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["body"]["source_type"], "manual_endpoint")
        mock_settings.assert_called_once()
        mock_parse.assert_called_once()
        mock_enqueue.assert_called_once()
        mock_discovery.assert_not_called()

    def test_vuln_trigger_passes_through_request_context_path(self) -> None:
        event = {
            "requestContext": {"http": {"path": "/events/vuln-trigger"}},
            "headers": {},
            "body": "",
        }
        with patch("lambda_discovery.handler", return_value={"action": "completed"}) as mock_handler:
            response = intake_handler(event, None)
        self.assertEqual(response["statusCode"], 200)
        mock_handler.assert_called_once_with({}, None)


if __name__ == "__main__":
    unittest.main()
