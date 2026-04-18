from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import post_issue_comment_once


class CommentDedupeTests(unittest.TestCase):
    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_skips_exact_duplicate_body(self, mock_github_request) -> None:
        mock_github_request.return_value = [
            {"id": 1, "body": "older"},
            {"id": 2, "body": "AWS poller status update.\n\n- Session ID: `abc`"},
        ]
        posted = post_issue_comment_once(
            {
                "owner": "C0smicCrush",
                "repo": "superset-remediation",
                "gh_token": "token",
            },
            73,
            "AWS poller status update.\n\n- Session ID: `abc`",
        )
        self.assertFalse(posted)
        self.assertEqual(mock_github_request.call_count, 1)

    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_posts_new_body(self, mock_github_request) -> None:
        mock_github_request.side_effect = [
            [{"id": 1, "body": "older"}],
            {"id": 2},
        ]
        posted = post_issue_comment_once(
            {
                "owner": "C0smicCrush",
                "repo": "superset-remediation",
                "gh_token": "token",
            },
            73,
            "AWS verification status update.\n\n- Session ID: `def`",
        )
        self.assertTrue(posted)
        self.assertEqual(mock_github_request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
