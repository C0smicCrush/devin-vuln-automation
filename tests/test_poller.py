from __future__ import annotations

import sys
import types
import unittest


fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = lambda *_args, **_kwargs: object()
sys.modules.setdefault("boto3", fake_boto3)

from lambda_poller import (
    _build_issue_rollups,
    _build_terminal_verdict_index,
    _build_update_lines,
    _effective_structured_output,
    _extract_pr_number,
    _previously_landed_terminal_verdict,
    _session_changed,
    _structured_output_is_final,
)


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

    def test_verification_verdict_is_final_even_when_status_is_still_running(self) -> None:
        """Devin occasionally leaves a verification session `status=running` after it has already
        committed a terminal `verdict` into structured_output. The poller must still treat that
        verdict as final for verification sessions so the verdict gets posted as a comment and
        counted in the rollups. Without this, a PR with a perfectly valid `verified` verdict
        sits there forever with no verdict comment."""
        session = {
            "session_id": "ver-abc",
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {
                "summary": "Independently verified the pyarrow bump.",
                "verdict": "verified",
                "issue_fixed": True,
            },
        }
        self.assertTrue(_structured_output_is_final(session, phase="verification"))
        self.assertEqual(
            _effective_structured_output(session, phase="verification").get("verdict"),
            "verified",
        )
        lines = _build_update_lines(session, "AWS verification status update.", phase="verification")
        self.assertTrue(any("Verdict: `verified`" in line for line in lines))
        self.assertTrue(any("Summary: Independently verified" in line for line in lines))

    def test_non_terminal_verdict_string_does_not_force_finality(self) -> None:
        """Guards against a malformed or mid-session `verdict` string unexpectedly marking a
        still-running verification as final. Only values in the published verdict enum qualify."""
        session = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {"verdict": "leaning toward verified"},
        }
        self.assertFalse(_structured_output_is_final(session, phase="verification"))
        self.assertEqual(_effective_structured_output(session, phase="verification"), {})

    def test_remediation_with_running_status_stays_provisional_even_with_terminal_result(self) -> None:
        """The phase-aware finality check must not leak back into remediation. A remediation
        session that is still `running` but has already written e.g. `result: manual_review`
        into structured_output is considered still working by design, because Devin can revise
        that result as it continues."""
        session = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {
                "summary": "Considering whether to proceed.",
                "result": "manual_review",
            },
        }
        self.assertFalse(_structured_output_is_final(session, phase="remediation"))
        self.assertEqual(_effective_structured_output(session, phase="remediation"), {})

    def test_session_changed_posts_verdict_when_verification_transitions_to_running_with_verdict(self) -> None:
        """Concrete case we saw on PR #111: the previous snapshot has an empty verdict (session
        was running with no verdict yet); the new snapshot still has status=running but now
        carries `verdict=verified` in structured_output. The poller must detect the change
        and post an update comment."""
        previous = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {"summary": "Running checks"},
        }
        current = {
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "structured_output": {
                "summary": "Running checks",
                "verdict": "verified",
                "issue_fixed": True,
            },
        }
        self.assertTrue(_session_changed(current, previous, phase="verification"))

    def test_verification_rollups_count_verdict_even_while_running(self) -> None:
        sessions = [
            {
                "phase": "verification",
                "issue_number": 110,
                "session_id": "ver-running-but-done",
                "status": "running",
                "status_detail": "working",
                "pull_requests": [],
                "structured_output": {
                    "summary": "Independently verified the pyarrow bump.",
                    "verdict": "verified",
                    "issue_fixed": True,
                },
                "tags": ["issue:110", "pr:111"],
            },
        ]
        rollups = _build_issue_rollups(sessions)
        self.assertEqual(rollups["verification_verdict_counts"]["verified"], 1)
        self.assertEqual(rollups["tracked_items_verified"], 1)

    def test_previously_landed_terminal_verdict_recognizes_four_enum_values(self) -> None:
        """Any of the four published verdicts in the previous snapshot counts as 'already narrated.'
        An empty verdict or anything outside the enum does not, so a mid-session provisional string
        can't accidentally freeze the bot."""
        for verdict in ("verified", "not_fixed", "partially_fixed", "not_verified"):
            self.assertEqual(
                _previously_landed_terminal_verdict({"structured_output": {"verdict": verdict}}),
                verdict,
                msg=f"expected {verdict!r} to count as terminal",
            )
        self.assertEqual(_previously_landed_terminal_verdict({"structured_output": {"verdict": ""}}), "")
        self.assertEqual(
            _previously_landed_terminal_verdict({"structured_output": {"verdict": "leaning verified"}}),
            "",
        )
        self.assertEqual(_previously_landed_terminal_verdict({}), "")
        self.assertEqual(_previously_landed_terminal_verdict(None), "")  # type: ignore[arg-type]

    def test_build_terminal_verdict_index_only_counts_previously_landed_verdicts(self) -> None:
        """The index is the 'handoff is old news' set: verification sessions that had a terminal
        verdict on the PRIOR poller tick. A verdict that only just landed on THIS tick is
        intentionally excluded, so the very tick where verification lands still allows the
        remediation session's final status comment to go through. Only subsequent ticks go silent."""
        verification_sessions = [
            {
                # Verdict landed last tick -> silence this issue/PR pair.
                "session_id": "ver-old",
                "tags": ["issue:110", "pr:111"],
                "status": "exit",
                "status_detail": None,
                "structured_output": {"summary": "Verified last tick", "verdict": "verified"},
            },
            {
                # Verdict just landed this tick -> NOT yet in the silence index, so the landing
                # tick still narrates the remediation session's final state.
                "session_id": "ver-new",
                "tags": ["issue:200", "pr:201"],
                "status": "running",
                "status_detail": "working",
                "structured_output": {"summary": "Just landed", "verdict": "verified"},
            },
            {
                # Still verifying, no verdict yet.
                "session_id": "ver-pending",
                "tags": ["issue:300", "pr:301"],
                "status": "running",
                "status_detail": "working",
                "structured_output": {"summary": "Still running"},
            },
        ]
        previous_by_session = {
            "ver-old": {"structured_output": {"verdict": "verified"}},
            "ver-new": {"structured_output": {"summary": "was running", "verdict": ""}},
            "ver-pending": {"structured_output": {}},
        }
        index = _build_terminal_verdict_index(verification_sessions, previous_by_session)
        self.assertEqual(index, {(110, 111): "verified"})

    def test_build_terminal_verdict_index_skips_untagged_sessions(self) -> None:
        verification_sessions = [
            {
                "session_id": "ver-no-pr-tag",
                "tags": ["issue:110"],
                "structured_output": {"verdict": "verified"},
            },
            {
                "session_id": "ver-no-issue-tag",
                "tags": ["pr:111"],
                "structured_output": {"verdict": "verified"},
            },
        ]
        previous_by_session = {
            "ver-no-pr-tag": {"structured_output": {"verdict": "verified"}},
            "ver-no-issue-tag": {"structured_output": {"verdict": "verified"}},
        }
        self.assertEqual(_build_terminal_verdict_index(verification_sessions, previous_by_session), {})

    def test_build_terminal_verdict_index_all_four_verdicts_silence_the_loop(self) -> None:
        """A `not_fixed` / `partially_fixed` / `not_verified` verdict is just as terminal as
        `verified` from the control plane's point of view: the decision has been committed and
        the loop has been handed to humans. The poller shouldn't keep narrating either way."""
        for verdict in ("verified", "not_fixed", "partially_fixed", "not_verified"):
            sessions = [
                {
                    "session_id": f"ver-{verdict}",
                    "tags": ["issue:500", "pr:501"],
                    "structured_output": {"verdict": verdict},
                }
            ]
            previous = {f"ver-{verdict}": {"structured_output": {"verdict": verdict}}}
            index = _build_terminal_verdict_index(sessions, previous)
            self.assertEqual(index, {(500, 501): verdict}, msg=f"expected {verdict!r} to silence")

    def test_handler_silences_remediation_comment_after_verdict_landed_on_prior_tick(self) -> None:
        """End-to-end contract: on poller tick N+1 after verification landed `verified` on tick N,
        the remediation session for the same (issue, PR) still exists in Devin's session list
        and _session_changed can still be true (e.g. Devin wrote a new status_detail), but we
        must NOT post a comment on the GitHub issue, because the decision has been handed to
        humans. Verification comments are also silenced once previously narrated. Metrics are
        still collected."""
        from unittest import mock

        import lambda_poller

        remediation_session = {
            "session_id": "rem-abc",
            "status": "exit",
            "status_detail": None,
            "tags": ["issue:110"],
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/111"}],
            "structured_output": {"summary": "Done, PR opened", "result": "pr_opened"},
        }
        verification_session = {
            "session_id": "ver-xyz",
            "status": "exit",
            "status_detail": None,
            "tags": ["issue:110", "pr:111"],
            "pull_requests": [],
            "structured_output": {"summary": "Verified the pyarrow bump", "verdict": "verified"},
        }
        previous_snapshot = {
            "sessions": [
                {
                    "session_id": "rem-abc",
                    "status": "running",
                    "status_detail": "working",
                    "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/111"}],
                    "structured_output": {"summary": "Working"},
                    "tags": ["issue:110"],
                },
                {
                    "session_id": "ver-xyz",
                    "status": "running",
                    "status_detail": "working",
                    "pull_requests": [],
                    "structured_output": {"summary": "Still verifying", "verdict": "verified"},
                    "tags": ["issue:110", "pr:111"],
                },
            ]
        }

        def fake_devin_request(method, path, **_kwargs):
            if path.endswith("/sessions/rem-abc"):
                return remediation_session
            if path.endswith("/sessions/ver-xyz"):
                return verification_session
            raise AssertionError(f"unexpected devin_request {method} {path}")

        def fake_list_project_sessions(_settings, phase=None):
            if phase == "remediation":
                return [{"session_id": "rem-abc"}]
            if phase == "verification":
                return [{"session_id": "ver-xyz"}]
            return []

        post_calls: list[tuple[int, str]] = []

        with mock.patch.object(lambda_poller, "load_runtime_settings", return_value={"devin_org_id": "org", "devin_api_key": "k"}), \
             mock.patch.object(lambda_poller, "_load_previous_snapshot", return_value=previous_snapshot), \
             mock.patch.object(lambda_poller, "_save_snapshot"), \
             mock.patch.object(lambda_poller, "store_metrics_snapshot"), \
             mock.patch.object(lambda_poller, "list_project_sessions", side_effect=fake_list_project_sessions), \
             mock.patch.object(lambda_poller, "has_verification_session_for_pr", return_value=True), \
             mock.patch.object(lambda_poller, "launch_verification_session"), \
             mock.patch.object(lambda_poller, "devin_request", side_effect=fake_devin_request), \
             mock.patch.object(lambda_poller, "_post_issue_comment", side_effect=lambda _s, n, b: post_calls.append((n, b))):
            metrics = lambda_poller.handler({}, None)

        self.assertEqual(
            post_calls,
            [],
            msg="no comments should be posted on the tick after a terminal verdict already landed",
        )
        self.assertEqual(metrics["total_sessions"], 2)
        self.assertEqual(metrics["verification_verdict_counts"]["verified"], 1)

    def test_handler_still_narrates_on_the_tick_where_verdict_first_lands(self) -> None:
        """On the landing tick, the previous snapshot still has an empty verdict, so the
        terminal-verdict index is empty, so both the remediation status comment AND the
        verification verdict comment should fire. Subsequent ticks go silent (covered in the
        previous test)."""
        from unittest import mock

        import lambda_poller

        remediation_session = {
            "session_id": "rem-abc",
            "status": "exit",
            "status_detail": None,
            "tags": ["issue:110"],
            "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/111"}],
            "structured_output": {"summary": "Done, PR opened", "result": "pr_opened"},
        }
        verification_session = {
            "session_id": "ver-xyz",
            "status": "running",
            "status_detail": "working",
            "tags": ["issue:110", "pr:111"],
            "pull_requests": [],
            "structured_output": {"summary": "Verified the pyarrow bump", "verdict": "verified"},
        }
        previous_snapshot = {
            "sessions": [
                {
                    "session_id": "rem-abc",
                    "status": "running",
                    "status_detail": "working",
                    "pull_requests": [{"pr_url": "https://github.com/example/repo/pull/111"}],
                    "structured_output": {"summary": "Working"},
                    "tags": ["issue:110"],
                },
                {
                    "session_id": "ver-xyz",
                    "status": "running",
                    "status_detail": "working",
                    "pull_requests": [],
                    "structured_output": {"summary": "Still verifying", "verdict": ""},
                    "tags": ["issue:110", "pr:111"],
                },
            ]
        }

        def fake_devin_request(_method, path, **_kwargs):
            if path.endswith("/sessions/rem-abc"):
                return remediation_session
            if path.endswith("/sessions/ver-xyz"):
                return verification_session
            raise AssertionError(f"unexpected {path}")

        def fake_list_project_sessions(_settings, phase=None):
            if phase == "remediation":
                return [{"session_id": "rem-abc"}]
            if phase == "verification":
                return [{"session_id": "ver-xyz"}]
            return []

        post_calls: list[tuple[int, str]] = []

        with mock.patch.object(lambda_poller, "load_runtime_settings", return_value={"devin_org_id": "org", "devin_api_key": "k"}), \
             mock.patch.object(lambda_poller, "_load_previous_snapshot", return_value=previous_snapshot), \
             mock.patch.object(lambda_poller, "_save_snapshot"), \
             mock.patch.object(lambda_poller, "store_metrics_snapshot"), \
             mock.patch.object(lambda_poller, "list_project_sessions", side_effect=fake_list_project_sessions), \
             mock.patch.object(lambda_poller, "has_verification_session_for_pr", return_value=True), \
             mock.patch.object(lambda_poller, "launch_verification_session"), \
             mock.patch.object(lambda_poller, "devin_request", side_effect=fake_devin_request), \
             mock.patch.object(lambda_poller, "_post_issue_comment", side_effect=lambda _s, n, b: post_calls.append((n, b))):
            lambda_poller.handler({}, None)

        commented_targets = [target for target, _body in post_calls]
        # Remediation session comments on the issue; verification session comments on both the
        # issue and the PR. So we expect: issue-110 (from remediation), issue-110 (from
        # verification), pr-111 (from verification) = 3 posts, 110 appearing twice.
        self.assertIn(110, commented_targets)
        self.assertIn(111, commented_targets)
        self.assertEqual(commented_targets.count(110), 2, msg="issue gets both remediation and verification comments on landing tick")
        self.assertEqual(commented_targets.count(111), 1, msg="PR gets the verification comment once")

    def test_handler_survives_a_devin_fetch_error_on_one_session(self) -> None:
        """Concrete incident shape: one Devin session's detail fetch errors (API 5xx, network
        hiccup, archived session returning bad JSON). Before this fix, the uncaught exception
        took the whole poller tick down and a perfectly good verdict on another session never
        posted. After the fix, the bad session is skipped with a log line and the healthy
        sessions still get processed."""
        from unittest import mock

        import lambda_poller

        healthy_verification = {
            "session_id": "ver-healthy",
            "status": "running",
            "status_detail": "working",
            "tags": ["issue:110", "pr:111"],
            "pull_requests": [],
            "structured_output": {"summary": "Verified the bump", "verdict": "verified"},
        }

        def fake_devin_request(_method, path, **_kwargs):
            if path.endswith("/sessions/ver-broken"):
                raise RuntimeError("devin api hiccup")
            if path.endswith("/sessions/ver-healthy"):
                return healthy_verification
            raise AssertionError(f"unexpected devin_request path {path}")

        def fake_list_project_sessions(_settings, phase=None):
            if phase == "remediation":
                return []
            if phase == "verification":
                return [{"session_id": "ver-broken"}, {"session_id": "ver-healthy"}]
            return []

        post_calls: list[tuple[int, str]] = []
        with mock.patch.object(lambda_poller, "load_runtime_settings", return_value={"devin_org_id": "org", "devin_api_key": "k"}), \
             mock.patch.object(lambda_poller, "_load_previous_snapshot", return_value={"sessions": []}), \
             mock.patch.object(lambda_poller, "_save_snapshot"), \
             mock.patch.object(lambda_poller, "store_metrics_snapshot"), \
             mock.patch.object(lambda_poller, "list_project_sessions", side_effect=fake_list_project_sessions), \
             mock.patch.object(lambda_poller, "has_verification_session_for_pr", return_value=True), \
             mock.patch.object(lambda_poller, "launch_verification_session"), \
             mock.patch.object(lambda_poller, "devin_request", side_effect=fake_devin_request), \
             mock.patch.object(lambda_poller, "_post_issue_comment", side_effect=lambda _s, n, b: post_calls.append((n, b))):
            metrics = lambda_poller.handler({}, None)

        # Healthy session's verdict should still land on both issue and PR.
        commented = [num for num, _body in post_calls]
        self.assertIn(110, commented)
        self.assertIn(111, commented)
        # Only the healthy session got processed into metrics; the broken one is silently dropped.
        self.assertEqual(metrics["total_sessions"], 1)
        self.assertEqual(metrics["verification_verdict_counts"]["verified"], 1)

    def test_handler_survives_a_post_comment_error_on_one_session(self) -> None:
        """Mirror of the Devin-fetch failure case but at the GitHub layer: posting a verdict
        comment for one session raises (e.g. auth hiccup, unexpected 5xx). The handler catches
        the exception, logs it, and keeps going so the other sessions still get their verdicts
        narrated. This is the containment layer — the 404-on-deleted-issue case is already
        swallowed inside `post_issue_comment_once`; this handles everything else."""
        from unittest import mock

        import lambda_poller
        from common import HttpStatusError

        session_broken = {
            "session_id": "ver-broken",
            "status": "running",
            "status_detail": "working",
            "tags": ["issue:999", "pr:999"],
            "pull_requests": [],
            "structured_output": {"summary": "Verified", "verdict": "verified"},
        }
        session_healthy = {
            "session_id": "ver-healthy",
            "status": "running",
            "status_detail": "working",
            "tags": ["issue:110", "pr:111"],
            "pull_requests": [],
            "structured_output": {"summary": "Verified the bump", "verdict": "verified"},
        }

        def fake_devin_request(_method, path, **_kwargs):
            if path.endswith("/sessions/ver-broken"):
                return session_broken
            if path.endswith("/sessions/ver-healthy"):
                return session_healthy
            raise AssertionError(path)

        def fake_list_project_sessions(_settings, phase=None):
            if phase == "verification":
                return [{"session_id": "ver-broken"}, {"session_id": "ver-healthy"}]
            return []

        post_calls: list[tuple[int, str]] = []

        def fake_post(_settings, number, body):
            if number == 999:
                raise HttpStatusError(
                    "POST",
                    "https://api.github.com/repos/C0smicCrush/superset-remediation/issues/999/comments",
                    500,
                    '{"message":"Server error"}',
                )
            post_calls.append((number, body))

        with mock.patch.object(lambda_poller, "load_runtime_settings", return_value={"devin_org_id": "org", "devin_api_key": "k"}), \
             mock.patch.object(lambda_poller, "_load_previous_snapshot", return_value={"sessions": []}), \
             mock.patch.object(lambda_poller, "_save_snapshot"), \
             mock.patch.object(lambda_poller, "store_metrics_snapshot"), \
             mock.patch.object(lambda_poller, "list_project_sessions", side_effect=fake_list_project_sessions), \
             mock.patch.object(lambda_poller, "has_verification_session_for_pr", return_value=True), \
             mock.patch.object(lambda_poller, "launch_verification_session"), \
             mock.patch.object(lambda_poller, "devin_request", side_effect=fake_devin_request), \
             mock.patch.object(lambda_poller, "_post_issue_comment", side_effect=fake_post):
            lambda_poller.handler({}, None)

        commented = [n for n, _b in post_calls]
        self.assertIn(110, commented)
        self.assertIn(111, commented)
        self.assertNotIn(999, commented, "broken session's comment did not get posted")

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
