from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.dashboard_server import build_dashboard_payload


class DashboardTests(unittest.TestCase):
    def test_build_dashboard_payload_reads_metrics_and_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            metrics_dir = Path(temp_dir) / "metrics"
            queue_dir = state_dir / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            metrics_dir.mkdir(parents=True, exist_ok=True)

            (queue_dir / "work_items.json").write_text(
                json.dumps(
                    [
                        {"message_id": "one", "body": {"title": "First"}},
                        {"message_id": "two", "body": {"title": "Second"}},
                    ]
                ),
                encoding="utf-8",
            )
            (metrics_dir / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": 123,
                        "active_sessions": 2,
                        "completed_sessions": 3,
                        "failed_sessions": 1,
                        "pull_requests_opened": 4,
                        "tracked_items_total": 2,
                        "tracked_items_verified": 1,
                        "verification_verdict_counts": {
                            "verified": 1,
                            "partially_fixed": 0,
                            "not_fixed": 1,
                            "not_verified": 0,
                        },
                        "sessions": [
                            {
                                "phase": "remediation",
                                "issue_number": 91,
                                "session_id": "sess-1",
                                "status": "running",
                                "status_detail": "working",
                                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/93"}],
                                "structured_output": {},
                            }
                        ],
                        "issue_rollups": [
                            {
                                "issue_number": 91,
                                "remediation_sessions": 1,
                                "verification_sessions": 1,
                                "latest_verdict": "verified",
                                "verified": True,
                                "human_info_requested": False,
                                "human_comment_followups": 0,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "LOCAL_STATE_DIR": str(state_dir),
                    "LOCAL_METRICS_DIR": str(metrics_dir),
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "GH_TOKEN": "",
                },
                clear=False,
            ):
                payload = build_dashboard_payload()

        self.assertEqual(payload["queue_depth"], 2)
        self.assertEqual(payload["overview"]["active_sessions"], 2)
        self.assertEqual(payload["overview"]["tracked_items_verified"], 1)
        self.assertEqual(payload["verification_verdict_counts"]["not_fixed"], 1)
        self.assertEqual(payload["recent_sessions"][0]["issue_url"], "https://github.com/example/repo/issues/91")
        self.assertEqual(payload["recent_sessions"][0]["pull_requests"][0]["number"], 93)
        self.assertEqual(payload["issue_rollups"][0]["latest_verdict"], "verified")
        self.assertFalse(payload["repo_analytics"]["computed_from_github"])
        self.assertEqual(payload["repo_analytics"]["error"], "")

    def test_build_dashboard_payload_computes_repo_analytics_from_github(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            metrics_dir = Path(temp_dir) / "metrics"
            queue_dir = state_dir / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            metrics_dir.mkdir(parents=True, exist_ok=True)

            (queue_dir / "work_items.json").write_text("[]", encoding="utf-8")
            (metrics_dir / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": 123,
                        "sessions": [
                            {
                                "phase": "remediation",
                                "issue_number": 91,
                                "session_id": "sess-1",
                                "status": "completed",
                                "status_detail": "done",
                                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/93"}],
                                "structured_output": {},
                            }
                        ],
                        "issue_rollups": [
                            {
                                "issue_number": 91,
                                "remediation_sessions": 2,
                                "verification_sessions": 1,
                                "latest_verdict": "verified",
                                "verified": True,
                                "human_info_requested": True,
                                "human_comment_followups": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def fake_github_request(method: str, path: str, token: str, query=None, payload=None):
                self.assertEqual(method, "GET")
                self.assertEqual(token, "token")
                if path == "/repos/example/repo/issues":
                    return [
                        {
                            "number": 91,
                            "body": "Tracked via automation.",
                            "state": "closed",
                            "created_at": "2026-04-15T00:00:00Z",
                            "closed_at": "2026-04-15T05:00:00Z",
                        }
                    ]
                if path == "/repos/example/repo/issues/91/comments":
                    return [
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS remediation worker launched Devin as the end-to-end remediation operator for this work item.\n\n- Session ID: `rem-1`",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "Human decision: please proceed with the recommended fix.",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS remediation worker launched Devin as the end-to-end remediation operator for this work item.\n\n- Session ID: `rem-2`",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS poller status update.\n\n- Session ID: `rem-2`\n- Pull request: https://github.com/example/repo/pull/93",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS poller launched a strict post-PR Devin verification review.\n\n- PR: https://github.com/example/repo/pull/93\n- Verification session ID: `ver-1`",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS verification status update.\n\n- Session ID: `ver-1`\n- Verdict: `verified`\n- Questions for human:\n  - none",
                        },
                    ]
                if path == "/repos/example/repo/issues/91/timeline":
                    return [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 93,
                                    "pull_request": {"url": "https://api.github.com/repos/example/repo/pulls/93"},
                                }
                            },
                        }
                    ]
                if path == "/repos/example/repo/pulls/93":
                    return {
                        "number": 93,
                        "state": "closed",
                        "created_at": "2026-04-15T02:00:00Z",
                        "closed_at": "2026-04-15T04:00:00Z",
                        "merged_at": "2026-04-15T04:00:00Z",
                    }
                raise AssertionError(f"Unexpected GitHub API call: {path}")

            def fake_devin_request(method: str, path: str, api_key: str, payload=None):
                self.assertEqual(method, "GET")
                self.assertEqual(api_key, "devin-token")
                self.assertEqual(
                    path,
                    "/v3/organizations/org-123/sessions?first=100&tags=project%3Adevin-vuln-automation",
                )
                return {
                    "items": [
                        {
                            "session_id": "rem-2",
                            "tags": ["project:devin-vuln-automation", "phase:remediation"],
                            "acus_consumed": 1.5,
                            "is_archived": False,
                        },
                        {
                            "session_id": "ver-1",
                            "tags": ["project:devin-vuln-automation", "phase:verification"],
                            "acus_consumed": 0.5,
                            "is_archived": False,
                        },
                    ]
                }

            with patch.dict(
                os.environ,
                {
                    "LOCAL_STATE_DIR": str(state_dir),
                    "LOCAL_METRICS_DIR": str(metrics_dir),
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "GH_TOKEN": "token",
                    "DEVIN_API_KEY": "devin-token",
                    "DEVIN_ORG_ID": "org-123",
                },
                clear=False,
            ), patch("scripts.dashboard_server.github_request", side_effect=fake_github_request), patch(
                "scripts.dashboard_server.devin_request",
                side_effect=fake_devin_request,
            ):
                payload = build_dashboard_payload()

        analytics = payload["repo_analytics"]
        self.assertTrue(analytics["computed_from_github"])
        self.assertEqual(payload["overview"]["tracked_items_total"], 1)
        self.assertEqual(payload["overview"]["tracked_items_verified"], 1)
        self.assertEqual(payload["overview"]["pull_requests_opened"], 1)
        self.assertEqual(payload["verification_verdict_counts"]["verified"], 1)
        self.assertEqual(payload["issue_rollups"][0]["latest_verdict"], "verified")
        self.assertEqual(
            {session["phase"] for session in payload["issue_rollups"][0]["sessions"]},
            {"remediation", "verification"},
        )
        self.assertEqual(analytics["tracked_issues_total"], 1)
        self.assertEqual(analytics["issues_with_pr"], 1)
        self.assertEqual(analytics["linked_prs_merged"], 1)
        self.assertEqual(analytics["attempted_issues_total"], 1)
        self.assertEqual(analytics["avg_remediation_iterations"], 1.0)
        self.assertEqual(analytics["avg_total_iterations"], 2.0)
        self.assertEqual(analytics["avg_human_followups"], 1.0)
        self.assertEqual(analytics["manual_intervention_rate"], 1.0)
        self.assertEqual(analytics["verified_issue_rate"], 1.0)
        self.assertEqual(analytics["avg_issue_to_first_pr_seconds"], 7200.0)
        self.assertEqual(analytics["avg_issue_to_resolution_seconds"], 18000.0)
        self.assertTrue(analytics["computed_from_devin"])
        self.assertEqual(analytics["tracked_devin_sessions_total"], 2)
        self.assertEqual(analytics["total_devin_acus"], 2.0)
        self.assertEqual(analytics["remediation_devin_acus"], 1.5)
        self.assertEqual(analytics["verification_devin_acus"], 0.5)
        self.assertEqual(
            analytics["daily_activity"],
            [
                {
                    "date": "2026-04-15",
                    "issues_created": 1,
                    "issues_closed": 1,
                    "prs_opened": 1,
                    "prs_merged": 1,
                    "prs_closed_unmerged": 0,
                }
            ],
        )

    def test_repo_analytics_excludes_closed_unmerged_prs_from_conversion_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            metrics_dir = Path(temp_dir) / "metrics"
            queue_dir = state_dir / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            metrics_dir.mkdir(parents=True, exist_ok=True)

            (queue_dir / "work_items.json").write_text("[]", encoding="utf-8")
            (metrics_dir / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": 123,
                        "sessions": [
                            {
                                "phase": "remediation",
                                "issue_number": 91,
                                "session_id": "sess-1",
                                "status": "completed",
                                "status_detail": "done",
                                "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/93"}],
                                "structured_output": {},
                            }
                        ],
                        "issue_rollups": [],
                    }
                ),
                encoding="utf-8",
            )

            def fake_github_request(method: str, path: str, token: str, query=None, payload=None):
                self.assertEqual(method, "GET")
                self.assertEqual(token, "token")
                if path == "/repos/example/repo/issues":
                    return [
                        {
                            "number": 91,
                            "body": "Tracked via automation.",
                            "state": "closed",
                            "created_at": "2026-04-15T00:00:00Z",
                            "closed_at": "2026-04-15T05:00:00Z",
                        }
                    ]
                if path == "/repos/example/repo/issues/91/comments":
                    return [
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS remediation worker launched Devin as the end-to-end remediation operator for this work item.\n\n- Session ID: `rem-1`",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS poller status update.\n\n- Session ID: `rem-1`\n- Pull request: https://github.com/example/repo/pull/93",
                        },
                    ]
                if path == "/repos/example/repo/issues/91/timeline":
                    return []
                if path == "/repos/example/repo/pulls/93":
                    return {
                        "number": 93,
                        "state": "closed",
                        "created_at": "2026-04-15T02:00:00Z",
                        "closed_at": "2026-04-15T03:00:00Z",
                        "merged_at": None,
                    }
                raise AssertionError(f"Unexpected GitHub API call: {path}")

            with patch.dict(
                os.environ,
                {
                    "LOCAL_STATE_DIR": str(state_dir),
                    "LOCAL_METRICS_DIR": str(metrics_dir),
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "GH_TOKEN": "token",
                },
                clear=False,
            ), patch("scripts.dashboard_server.github_request", side_effect=fake_github_request):
                payload = build_dashboard_payload()

        analytics = payload["repo_analytics"]
        self.assertTrue(analytics["computed_from_github"])
        self.assertEqual(payload["verification_verdict_counts"]["not_verified"], 1)
        self.assertEqual(payload["overview"]["tracked_items_verified"], 0)
        self.assertEqual(analytics["tracked_issues_total"], 1)
        self.assertEqual(analytics["issues_with_pr"], 0)
        self.assertEqual(analytics["issues_without_pr"], 1)
        self.assertEqual(analytics["linked_prs_total"], 1)
        self.assertEqual(analytics["linked_prs_closed_unmerged"], 1)
        self.assertEqual(analytics["linked_prs_merged"], 0)
        self.assertEqual(analytics["avg_remediation_iterations"], 1.0)
        self.assertEqual(analytics["avg_total_iterations"], 1.0)
        self.assertEqual(analytics["avg_issue_to_first_pr_seconds"], None)
        self.assertEqual(analytics["issue_to_pr_conversion_rate"], 0.0)
        self.assertEqual(
            analytics["daily_activity"],
            [
                {
                    "date": "2026-04-15",
                    "issues_created": 1,
                    "issues_closed": 1,
                    "prs_opened": 1,
                    "prs_merged": 0,
                    "prs_closed_unmerged": 1,
                }
            ],
        )

    def test_dashboard_prefers_open_issue_when_pr_cross_references_multiple_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            metrics_dir = Path(temp_dir) / "metrics"
            queue_dir = state_dir / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            metrics_dir.mkdir(parents=True, exist_ok=True)

            (queue_dir / "work_items.json").write_text("[]", encoding="utf-8")
            (metrics_dir / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": 123,
                        "sessions": [],
                        "issue_rollups": [],
                    }
                ),
                encoding="utf-8",
            )

            def fake_github_request(method: str, path: str, token: str, query=None, payload=None):
                self.assertEqual(method, "GET")
                self.assertEqual(token, "token")
                if path == "/repos/example/repo/issues":
                    return [
                        {
                            "number": 104,
                            "title": "Old soft delete thread",
                            "body": "Superseded thread.",
                            "state": "closed",
                            "created_at": "2026-04-18T00:00:00Z",
                            "closed_at": "2026-04-19T00:00:00Z",
                        },
                        {
                            "number": 106,
                            "title": "New soft delete thread",
                            "body": "Clean restart thread.",
                            "state": "open",
                            "created_at": "2026-04-19T01:00:00Z",
                            "closed_at": None,
                        },
                    ]
                if path in {
                    "/repos/example/repo/issues/104/comments",
                    "/repos/example/repo/issues/106/comments",
                }:
                    issue_number = 104 if "/104/" in path else 106
                    return [
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": (
                                "AWS remediation worker launched Devin as the end-to-end remediation operator "
                                f"for this work item.\n\n- Session ID: `rem-{issue_number}`"
                            ),
                        }
                    ]
                if path in {
                    "/repos/example/repo/issues/104/timeline",
                    "/repos/example/repo/issues/106/timeline",
                }:
                    return [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 107,
                                    "pull_request": {"url": "https://api.github.com/repos/example/repo/pulls/107"},
                                }
                            },
                        }
                    ]
                if path == "/repos/example/repo/pulls/107":
                    return {
                        "number": 107,
                        "state": "open",
                        "created_at": "2026-04-19T02:00:00Z",
                        "closed_at": None,
                        "merged_at": None,
                    }
                raise AssertionError(f"Unexpected GitHub API call: {path}")

            with patch.dict(
                os.environ,
                {
                    "LOCAL_STATE_DIR": str(state_dir),
                    "LOCAL_METRICS_DIR": str(metrics_dir),
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "GH_TOKEN": "token",
                },
                clear=False,
            ), patch("scripts.dashboard_server.github_request", side_effect=fake_github_request):
                payload = build_dashboard_payload()

        issues_by_number = {issue["issue_number"]: issue for issue in payload["issue_rollups"]}
        self.assertEqual(payload["overview"]["pull_requests_opened"], 1)
        self.assertEqual(payload["repo_analytics"]["issues_with_pr"], 1)
        self.assertEqual(payload["repo_analytics"]["linked_prs_total"], 1)
        self.assertEqual(issues_by_number[104]["pull_requests"], [])
        self.assertEqual(
            issues_by_number[106]["pull_requests"],
            [{"url": "https://github.com/example/repo/pull/107", "number": 107}],
        )

    def test_closed_verified_issue_does_not_show_current_human_followup_from_stale_waiting_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            metrics_dir = Path(temp_dir) / "metrics"
            queue_dir = state_dir / "queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            metrics_dir.mkdir(parents=True, exist_ok=True)

            (queue_dir / "work_items.json").write_text("[]", encoding="utf-8")
            (metrics_dir / "latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": 123,
                        "sessions": [],
                        "issue_rollups": [],
                    }
                ),
                encoding="utf-8",
            )

            def fake_github_request(method: str, path: str, token: str, query=None, payload=None):
                self.assertEqual(method, "GET")
                self.assertEqual(token, "token")
                if path == "/repos/example/repo/issues":
                    return [
                        {
                            "number": 91,
                            "title": "Closed verified issue",
                            "body": "Tracked via automation.",
                            "state": "closed",
                            "created_at": "2026-04-15T00:00:00Z",
                            "closed_at": "2026-04-15T05:00:00Z",
                        }
                    ]
                if path == "/repos/example/repo/issues/91/comments":
                    return [
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": "AWS remediation worker launched Devin as the end-to-end remediation operator for this work item.\n\n- Session ID: `rem-1`",
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": (
                                "AWS poller status update.\n\n"
                                "- Session ID: `rem-1`\n"
                                "- Status: `running`\n"
                                "- Detail: `waiting_for_user`\n"
                                "- Pull request: https://github.com/example/repo/pull/93"
                            ),
                        },
                        {
                            "author": {"login": "C0smicCrush"},
                            "body": (
                                "AWS verification status update.\n\n"
                                "- Session ID: `ver-1`\n"
                                "- Status: `exit`\n"
                                "- Verdict: `verified`\n"
                                "- Summary: Fix independently verified."
                            ),
                        },
                    ]
                if path == "/repos/example/repo/issues/91/timeline":
                    return [
                        {
                            "event": "cross-referenced",
                            "source": {
                                "issue": {
                                    "number": 93,
                                    "pull_request": {"url": "https://api.github.com/repos/example/repo/pulls/93"},
                                }
                            },
                        }
                    ]
                if path == "/repos/example/repo/pulls/93":
                    return {
                        "number": 93,
                        "state": "closed",
                        "created_at": "2026-04-15T02:00:00Z",
                        "closed_at": "2026-04-15T04:00:00Z",
                        "merged_at": "2026-04-15T04:00:00Z",
                    }
                raise AssertionError(f"Unexpected GitHub API call: {path}")

            with patch.dict(
                os.environ,
                {
                    "LOCAL_STATE_DIR": str(state_dir),
                    "LOCAL_METRICS_DIR": str(metrics_dir),
                    "TARGET_REPO_OWNER": "example",
                    "TARGET_REPO_NAME": "repo",
                    "GH_TOKEN": "token",
                    "DEVIN_API_KEY": "",
                    "DEVIN_ORG_ID": "",
                },
                clear=False,
            ), patch("scripts.dashboard_server.github_request", side_effect=fake_github_request):
                payload = build_dashboard_payload()

        self.assertEqual(payload["overview"]["tracked_items_total"], 1)
        self.assertEqual(payload["overview"]["tracked_items_verified"], 1)
        self.assertEqual(payload["overview"]["tracked_items_needing_human_followup"], 0)
        self.assertEqual(payload["repo_analytics"]["manual_intervention_rate"], 0.0)
        self.assertFalse(payload["issue_rollups"][0]["human_info_requested"])


if __name__ == "__main__":
    unittest.main()
