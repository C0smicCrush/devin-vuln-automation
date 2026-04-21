from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from aws_runtime import post_issue_comment_once
from common import HttpStatusError


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

    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_swallows_404_on_deleted_issue(self, mock_github_request) -> None:
        """Concrete incident: a human deletes issue #104 on the target repo, but a long-lived
        Devin session still carries the `issue:104` tag. The poller asks GitHub for comments on
        #104 and gets a 404. Previously this SystemExit'd out of the whole lambda and torpedoed
        every subsequent session, including a separate PR whose verification verdict was ready
        to land. Now the function logs the miss and returns False, so the poller marches on."""
        mock_github_request.side_effect = HttpStatusError(
            "GET",
            "https://api.github.com/repos/C0smicCrush/superset-remediation/issues/104/comments",
            404,
            '{"message":"Not Found"}',
        )
        posted = post_issue_comment_once(
            {"owner": "C0smicCrush", "repo": "superset-remediation", "gh_token": "token"},
            104,
            "AWS poller status update.",
        )
        self.assertFalse(posted)
        self.assertEqual(mock_github_request.call_count, 1, "should give up after the 404 list")

    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_swallows_410_on_deleted_issue(self, mock_github_request) -> None:
        """Gone (410) is semantically 'this resource used to exist here'. Treat it the same as
        404 — there's nothing to comment on and re-raising would crash the poller tick."""
        mock_github_request.side_effect = HttpStatusError(
            "GET",
            "https://api.github.com/repos/C0smicCrush/superset-remediation/issues/104/comments",
            410,
            '{"message":"Gone"}',
        )
        self.assertFalse(
            post_issue_comment_once(
                {"owner": "C0smicCrush", "repo": "superset-remediation", "gh_token": "token"},
                104,
                "AWS poller status update.",
            )
        )

    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_reraises_unexpected_http_errors(self, mock_github_request) -> None:
        """Auth failures (401/403), rate limits (429), and server errors (5xx) are real problems
        the operator needs to see, not quietly-deleted issues. Keep the loud crash for those."""
        mock_github_request.side_effect = HttpStatusError(
            "GET",
            "https://api.github.com/repos/C0smicCrush/superset-remediation/issues/73/comments",
            403,
            '{"message":"Bad credentials"}',
        )
        with self.assertRaises(HttpStatusError) as cm:
            post_issue_comment_once(
                {"owner": "C0smicCrush", "repo": "superset-remediation", "gh_token": "token"},
                73,
                "AWS poller status update.",
            )
        self.assertEqual(cm.exception.status_code, 403)

    @patch("aws_runtime.github_request")
    def test_post_issue_comment_once_swallows_404_on_post_after_successful_list(self, mock_github_request) -> None:
        """Race: the list call succeeded (issue still existed), but someone deleted it in the
        millisecond before we POST'd. Treat the same as an initial 404."""
        mock_github_request.side_effect = [
            [{"id": 1, "body": "older"}],
            HttpStatusError(
                "POST",
                "https://api.github.com/repos/C0smicCrush/superset-remediation/issues/104/comments",
                404,
                '{"message":"Not Found"}',
            ),
        ]
        self.assertFalse(
            post_issue_comment_once(
                {"owner": "C0smicCrush", "repo": "superset-remediation", "gh_token": "token"},
                104,
                "new body",
            )
        )
        self.assertEqual(mock_github_request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
