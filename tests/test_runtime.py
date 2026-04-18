from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import poll_session_until_terminal


class RuntimeTests(unittest.TestCase):
    @patch("aws_runtime.time.sleep", return_value=None)
    @patch("aws_runtime.devin_request")
    def test_poll_session_returns_waiting_for_user_with_structured_output(self, mock_devin_request, _mock_sleep) -> None:
        mock_devin_request.return_value = {
            "session_id": "sess-1",
            "status": "waiting_for_user",
            "structured_output": {"summary": "Comment follow-up is actionable"},
        }
        session = poll_session_until_terminal(
            {
                "devin_org_id": "org-test",
                "devin_api_key": "api-test",
            },
            "sess-1",
            timeout_seconds=1,
        )
        self.assertEqual(session["status"], "waiting_for_user")
        self.assertEqual(session["structured_output"]["summary"], "Comment follow-up is actionable")


if __name__ == "__main__":
    unittest.main()
