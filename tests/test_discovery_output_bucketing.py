from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.run_devin_discovery import (
    has_active_discovery_session,
    list_project_sessions,
    summarize_issue_creation,
)


class SummarizeIssueCreationTests(unittest.TestCase):
    def test_buckets_by_status_reported_by_devin(self) -> None:
        findings = [
            {
                "id": "F-1",
                "issue_creation_status": "opened",
                "issue_url": "https://github.com/o/r/issues/101",
                "issue_number": 101,
            },
            {
                "id": "F-2",
                "issue_creation_status": "duplicate_skipped",
                "issue_url": "https://github.com/o/r/issues/77",
                "issue_number": 77,
            },
            {
                "id": "F-3",
                "issue_creation_status": "failed",
                "issue_creation_error": "HTTP 403 insufficient permissions",
            },
        ]
        buckets = summarize_issue_creation(findings)
        self.assertEqual([rec["finding_id"] for rec in buckets["opened"]], ["F-1"])
        self.assertEqual(buckets["opened"][0]["issue_number"], 101)
        self.assertEqual([rec["finding_id"] for rec in buckets["duplicate_skipped"]], ["F-2"])
        self.assertEqual([rec["finding_id"] for rec in buckets["failed"]], ["F-3"])
        self.assertEqual(buckets["failed"][0]["issue_creation_error"], "HTTP 403 insufficient permissions")
        self.assertEqual(buckets["missing"], [])

    def test_missing_status_is_flagged(self) -> None:
        findings = [
            {"id": "F-broken", "title": "Devin forgot to report status"},
        ]
        buckets = summarize_issue_creation(findings)
        self.assertEqual([rec["finding_id"] for rec in buckets["missing"]], ["F-broken"])
        self.assertEqual(buckets["opened"], [])
        self.assertEqual(buckets["duplicate_skipped"], [])
        self.assertEqual(buckets["failed"], [])

    def test_empty_findings_returns_empty_buckets(self) -> None:
        buckets = summarize_issue_creation([])
        self.assertEqual(buckets, {"opened": [], "duplicate_skipped": [], "failed": [], "missing": []})


class DiscoveryPhaseFilterTests(unittest.TestCase):
    """The Devin sessions API accepts `tags=project:X&tags=phase:discovery` but in practice does
    NOT AND-combine the filters: it happily returns sessions tagged with just `project:X` and
    a different `phase:*`. If the client doesn't re-filter by phase tag, a running remediation
    session appears in the discovery list and `has_active_discovery_session` falsely returns
    True, which blocks every new /vuln-trigger with `existing_discovery_session`. Concrete
    incident: issue #112 was auto-created from a spurious work item and its remediation session
    was `running/waiting_for_user`, which made the discovery lambda refuse to launch."""

    SERVER_OVERFETCH = [
        {
            "session_id": "disc-suspended",
            "status": "suspended",
            "status_detail": "inactivity",
            "tags": ["project:devin-vuln-automation", "phase:discovery", "repo:superset-remediation"],
        },
        {
            "session_id": "rem-running",
            "status": "running",
            "status_detail": "waiting_for_user",
            "tags": ["project:devin-vuln-automation", "phase:remediation", "issue:112"],
        },
        {
            "session_id": "ver-running",
            "status": "running",
            "status_detail": "working",
            "tags": ["project:devin-vuln-automation", "phase:verification", "issue:110", "pr:111"],
        },
        {
            "session_id": "disc-running",
            "status": "running",
            "status_detail": "working",
            "tags": ["project:devin-vuln-automation", "phase:discovery"],
        },
    ]

    @patch("scripts.run_devin_discovery.devin_request")
    def test_list_project_sessions_drops_cross_phase_results_from_server(self, mock_devin_request) -> None:
        """The server returned 4 sessions including 2 non-discovery phases. After client-side
        filtering we should see only the 2 discovery sessions."""
        mock_devin_request.return_value = {"items": self.SERVER_OVERFETCH}
        result = list_project_sessions("org", "key", "discovery")
        returned_ids = {s["session_id"] for s in result}
        self.assertEqual(returned_ids, {"disc-suspended", "disc-running"})

    @patch("scripts.run_devin_discovery.devin_request")
    def test_has_active_discovery_session_true_when_discovery_is_running(self, mock_devin_request) -> None:
        mock_devin_request.return_value = {"items": self.SERVER_OVERFETCH}
        self.assertTrue(has_active_discovery_session("org", "key"))

    @patch("scripts.run_devin_discovery.devin_request")
    def test_has_active_discovery_session_false_when_only_non_discovery_phases_active(self, mock_devin_request) -> None:
        """Regression guard: a running remediation session must not count as an active discovery
        session. Before the client-side filter, this is what wedged /vuln-trigger in prod."""
        only_non_discovery_active = [
            s for s in self.SERVER_OVERFETCH if s["session_id"] != "disc-running"
        ]
        mock_devin_request.return_value = {"items": only_non_discovery_active}
        self.assertFalse(has_active_discovery_session("org", "key"))

    @patch("scripts.run_devin_discovery.devin_request")
    def test_has_active_discovery_session_false_when_no_sessions(self, mock_devin_request) -> None:
        mock_devin_request.return_value = {"items": []}
        self.assertFalse(has_active_discovery_session("org", "key"))


if __name__ == "__main__":
    unittest.main()
