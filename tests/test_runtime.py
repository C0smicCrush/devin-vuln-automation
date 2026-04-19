from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import dequeue_work_item, enqueue_work_item, load_runtime_settings, poll_session_until_terminal, store_metrics_snapshot


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

    def test_load_runtime_settings_from_local_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "RUNTIME_BACKEND": "local",
                    "GH_TOKEN": "gh-token",
                    "DEVIN_API_KEY": "devin-key",
                    "DEVIN_ORG_ID": "org-test",
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "LOCAL_STATE_DIR": str(Path(temp_dir) / "state"),
                    "LOCAL_METRICS_DIR": str(Path(temp_dir) / "metrics"),
                },
                clear=False,
            ):
                settings = load_runtime_settings()
        self.assertEqual(settings["backend"], "local")
        self.assertEqual(settings["owner"], "example")
        self.assertEqual(settings["repo"], "repo")

    def test_local_queue_round_trip_and_metrics_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = {
                "backend": "local",
                "local_state_dir": str(Path(temp_dir) / "state"),
                "local_metrics_dir": str(Path(temp_dir) / "metrics"),
            }
            message = {
                "source": {"type": "manual_endpoint"},
                "family_key": "dompurify",
                "title": "Investigate issue",
            }
            queued = enqueue_work_item(settings, message)
            popped = dequeue_work_item(settings)
            self.assertEqual(popped["message_id"], queued["message_id"])
            self.assertEqual(popped["body"]["title"], "Investigate issue")

            snapshot = {"status": "ok", "active_sessions": 0}
            store_metrics_snapshot(settings, snapshot)
            metrics_path = Path(temp_dir) / "metrics" / "latest.json"
            self.assertTrue(metrics_path.exists())
            self.assertIn('"status": "ok"', metrics_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
