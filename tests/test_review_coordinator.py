from __future__ import annotations

import unittest
from unittest import mock

from codey.agent import RunResult
from codey.execution_evidence import CheckEvidence
from codey.provider_diagnostics import ProviderActionError, ProviderFailure
from codey.review import ReviewFinding, ReviewResult
from codey.review_coordinator import ReviewCoordinator, change_state


CHANGES = {
    "ok": True,
    "changed_count": 1,
    "files": [{"path": "app.py", "status": "M"}],
    "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
}


class ReviewCoordinatorTests(unittest.TestCase):
    def run_cycle(self, **overrides):
        collect_changes = overrides.pop("collect_changes", mock.Mock(return_value=CHANGES))
        coordinator = ReviewCoordinator(collect_changes)
        run_review = overrides.pop(
            "run_review",
            mock.Mock(return_value=("mimo", ReviewResult("approved", "ok", []))),
        )
        close_writer = overrides.pop("close_writer_for_review", mock.Mock())
        repair_writer = overrides.pop(
            "repair_writer",
            mock.Mock(return_value=RunResult("fixed", "done", 1, False, True)),
        )
        set_checkpoint_status = overrides.pop("set_checkpoint_status", mock.Mock())
        emit_unavailable = overrides.pop("emit_review_unavailable", mock.Mock())
        refresh_project_map = overrides.pop(
            "refresh_project_map",
            mock.Mock(return_value="Project Map"),
        )
        build_verification_map = overrides.pop(
            "build_verification_map",
            mock.Mock(return_value="Verification Map"),
        )
        stop_requested = overrides.pop("stop_requested", mock.Mock(return_value=False))
        result = coordinator.run_cycle(
            project="project",
            tracker=object(),
            session_id="session",
            task="Fix app",
            result=overrides.pop("result", RunResult("done", "done", 1, False, True)),
            task_changed=overrides.pop("task_changed", True),
            changes=overrides.pop("changes", CHANGES),
            changes_dirty=overrides.pop("changes_dirty", False),
            writer_id="deepseek",
            recent_log="read app.py",
            render_change_brief=overrides.pop("render_change_brief", mock.Mock(return_value="brief")),
            execution_evidence="evidence",
            successful_checks=overrides.pop(
                "successful_checks",
                (CheckEvidence("python -m pytest", "."),),
            ),
            checkpoint_prompt=overrides.pop("checkpoint_prompt", "checkpoint prompt"),
            checks_before_review_followup=overrides.pop(
                "checks_before_review_followup",
                True,
            ),
            stop_requested=stop_requested,
            refresh_project_map=refresh_project_map,
            build_verification_map=build_verification_map,
            run_review=run_review,
            close_writer_for_review=close_writer,
            repair_writer=repair_writer,
            set_checkpoint_status=set_checkpoint_status,
            emit_review_unavailable=emit_unavailable,
        )
        return {
            "result": result,
            "collect_changes": collect_changes,
            "run_review": run_review,
            "close_writer": close_writer,
            "repair_writer": repair_writer,
            "set_checkpoint_status": set_checkpoint_status,
            "emit_unavailable": emit_unavailable,
            "refresh_project_map": refresh_project_map,
            "build_verification_map": build_verification_map,
        }

    def test_change_state_reports_unavailable_diff(self) -> None:
        self.assertIsNone(change_state({"ok": False, "error": "unavailable"}))
        self.assertFalse(change_state({"ok": True, "changed_count": 0, "files": []}))
        self.assertTrue(change_state(CHANGES))

    def test_no_changed_task_skips_review(self) -> None:
        state = self.run_cycle(task_changed=False, changes_dirty=False)

        self.assertFalse(state["result"].review_attempted)
        state["run_review"].assert_not_called()
        state["close_writer"].assert_not_called()

    def test_dirty_diff_retries_before_review(self) -> None:
        collect_changes = mock.Mock(return_value=CHANGES)

        state = self.run_cycle(
            collect_changes=collect_changes,
            changes={"ok": False, "error": "snapshot unavailable"},
            changes_dirty=True,
        )

        collect_changes.assert_called_once()
        state["run_review"].assert_called_once()
        self.assertTrue(state["result"].review_attempted)
        self.assertFalse(state["result"].changes_dirty)
        self.assertIs(state["result"].changes, CHANGES)

    def test_non_reviewable_diff_skips_review_after_map_refresh(self) -> None:
        changes = {"ok": True, "changed_count": 1, "files": [{"path": "app.py"}], "diff": ""}
        render_change_brief = mock.Mock(return_value="brief")

        state = self.run_cycle(changes=changes, render_change_brief=render_change_brief)

        state["refresh_project_map"].assert_called_once()
        render_change_brief.assert_not_called()
        state["run_review"].assert_not_called()
        state["close_writer"].assert_not_called()

    def test_review_unavailable_emits_and_preserves_writer_result(self) -> None:
        run_review = mock.Mock(side_effect=RuntimeError("reviewer unavailable"))

        state = self.run_cycle(run_review=run_review)

        state["emit_unavailable"].assert_called_once()
        state["repair_writer"].assert_not_called()
        self.assertTrue(state["result"].review_attempted)
        self.assertEqual(state["result"].result.summary, "done")

    def test_approved_review_does_not_repair(self) -> None:
        state = self.run_cycle()

        state["close_writer"].assert_called_once()
        state["repair_writer"].assert_not_called()
        self.assertTrue(state["result"].review_attempted)
        self.assertFalse(state["result"].review_repair_attempted)

    def test_rejected_review_runs_repair_and_marks_diff_dirty(self) -> None:
        review = ReviewResult(
            "changes_requested",
            "Fix one issue",
            [ReviewFinding("app.py", "Missing empty case", "Add a guard")],
        )
        run_review = mock.Mock(return_value=("mimo", review))
        repair_writer = mock.Mock(return_value=RunResult("fixed", "done", 2, False, True))

        state = self.run_cycle(run_review=run_review, repair_writer=repair_writer)

        self.assertEqual(
            [call.args[0] for call in state["set_checkpoint_status"].call_args_list],
            ["fixing_review", "ready_for_review"],
        )
        followup, checkpoint = repair_writer.call_args.args
        self.assertIn("Missing empty case", followup)
        self.assertEqual(checkpoint.prompt, "checkpoint prompt")
        self.assertEqual(checkpoint.changed_files, ("app.py",))
        self.assertEqual(checkpoint.successful_checks[0].command, "python -m pytest")
        self.assertTrue(state["result"].changes_dirty)
        self.assertTrue(state["result"].review_repair_attempted)
        self.assertEqual(state["result"].result.summary, "fixed")
        self.assertTrue(state["result"].task_changed)
        state["refresh_project_map"].assert_called_once()

    def test_review_repair_without_changes_can_inherit_prior_green_check(self) -> None:
        review = ReviewResult("changes_requested", "Check claim", [])
        repair_writer = mock.Mock(
            return_value=RunResult("claim invalid", "done", 1, False, False, False)
        )

        state = self.run_cycle(
            run_review=mock.Mock(return_value=("mimo", review)),
            repair_writer=repair_writer,
            checks_before_review_followup=True,
        )

        self.assertTrue(state["result"].result.checks_passed)
        self.assertTrue(state["result"].changes_dirty)

    def test_review_repair_failed_check_does_not_inherit_green_check(self) -> None:
        review = ReviewResult("changes_requested", "Check claim", [])
        repair_writer = mock.Mock(
            return_value=RunResult("tests failed", "done", 1, False, False, True)
        )

        state = self.run_cycle(
            run_review=mock.Mock(return_value=("mimo", review)),
            repair_writer=repair_writer,
            checks_before_review_followup=True,
        )

        self.assertFalse(state["result"].result.checks_passed)
        self.assertTrue(state["result"].changes_dirty)

    def test_review_repair_no_progress_does_not_inherit_green_check(self) -> None:
        review = ReviewResult("changes_requested", "Check claim", [])
        repair_writer = mock.Mock(
            return_value=RunResult("no progress", "no_progress", 1, False, False, False)
        )

        state = self.run_cycle(
            run_review=mock.Mock(return_value=("mimo", review)),
            repair_writer=repair_writer,
            checks_before_review_followup=True,
        )

        self.assertFalse(state["result"].result.checks_passed)
        self.assertEqual(state["result"].result.stop_reason, "no_progress")

    def test_repair_provider_failure_propagates(self) -> None:
        review = ReviewResult("changes_requested", "Fix it", [])
        failure = ProviderFailure(
            "DeepSeek",
            "send",
            "",
            "",
            "response missing",
            "now",
            "response_missing",
        )

        with self.assertRaises(ProviderActionError):
            self.run_cycle(
                run_review=mock.Mock(return_value=("mimo", review)),
                repair_writer=mock.Mock(side_effect=ProviderActionError(failure)),
            )

    def test_stop_requested_skips_review(self) -> None:
        state = self.run_cycle(stop_requested=mock.Mock(return_value=True))

        state["run_review"].assert_not_called()
        state["close_writer"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
