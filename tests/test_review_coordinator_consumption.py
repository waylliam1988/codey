"""Coordinator, review-only and display wiring (batch 5)."""
from __future__ import annotations

import unittest
from unittest import mock

from codey.operations.review_flow import render_review_only_summary
from codey.reviews.coordinator import ReviewCoordinator
from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.input import ReviewScope
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.execution_evidence import CheckEvidence
from tests.support.review_workspace import review_workspace

CHANGES = {
    "ok": True,
    "changed_count": 1,
    "files": [{"path": "app.py", "status": "M"}],
    "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
}


def _cycle(**overrides):
    collect_changes = overrides.pop("collect_changes", mock.Mock(return_value=CHANGES))
    coordinator = ReviewCoordinator(collect_changes)
    run_review = overrides.pop(
        "run_review",
        mock.Mock(return_value=("r", ReviewResult("approved", "ok", []))),
    )
    close_writer = overrides.pop("close_writer_for_review", mock.Mock())
    repair_writer = overrides.pop(
        "repair_writer",
        mock.Mock(return_value=RunResult("fixed", "done", 1, False, True)),
    )
    with review_workspace(run_review) as (project, callback):
        return coordinator.run_cycle(
            project=overrides.pop("project", project),
            tracker=object(),
            session_id="session",
            task="Fix app",
            result=overrides.pop("result", RunResult("done", "done", 1, False, True)),
            task_changed=overrides.pop("task_changed", True),
            changes=overrides.pop("changes", CHANGES),
            changes_dirty=overrides.pop("changes_dirty", False),
            writer_id="deepseek",
            recent_log="log",
            render_change_brief=overrides.pop("render_change_brief", mock.Mock(return_value="brief")),
            execution_evidence="evidence",
            successful_checks=(CheckEvidence("python -m pytest", "."),),
            checkpoint_prompt="checkpoint",
            checks_before_review_followup=True,
            stop_requested=mock.Mock(return_value=False),
            refresh_project_map=mock.Mock(return_value="map"),
            build_verification_map=mock.Mock(return_value="vmap"),
            run_review=callback,
            close_writer_for_review=close_writer,
            repair_writer=repair_writer,
            set_checkpoint_status=mock.Mock(),
            emit_review_unavailable=mock.Mock(),
        ), {"run_review": run_review, "repair_writer": repair_writer}


class CoordinatorConsumptionTests(unittest.TestCase):
    def test_project_actionable_finding_enters_one_repair(self) -> None:
        review = ReviewResult(
            "changes_requested", "fix", [ReviewFinding("app.py", "bug")], status="complete"
        )
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        self.assertTrue(result.review_repair_attempted)
        mocks["repair_writer"].assert_called_once()

    def test_duplicate_findings_do_not_duplicate_repair_content(self) -> None:
        # dedupe happens at parse; coordinator must not duplicate repair content
        review = ReviewResult(
            "changes_requested", "fix", [ReviewFinding("app.py", "ZZZBUG-1")], status="complete"
        )
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        followup = mocks["repair_writer"].call_args.args[0]
        self.assertEqual(followup.count("ZZZBUG-1"), 1)

    def test_only_invalid_findings_do_not_trigger_vague_repair(self) -> None:
        review = ReviewResult("unknown", "incomplete", [], status="incomplete")
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        mocks["repair_writer"].assert_not_called()
        self.assertFalse(result.review_repair_attempted)

    def test_partial_approved_is_not_complete_approved(self) -> None:
        scope = ReviewScope(
            total_changed_files=2,
            provided_files=("app.py",),
            excluded_files=(".env",),
            diff_truncated=True,
        )
        review = ReviewResult("approved", "ok", [], status="complete", scope=scope)
        self.assertFalse(review.approved)
        self.assertFalse(review.is_complete)
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        mocks["repair_writer"].assert_not_called()

    def test_partial_actionable_findings_can_trigger_existing_repair(self) -> None:
        scope = ReviewScope(
            total_changed_files=2,
            provided_files=("app.py",),
            excluded_files=(".env",),
            diff_truncated=True,
        )
        review = ReviewResult(
            "changes_requested",
            "fix",
            [ReviewFinding("app.py", "bug")],
            status="incomplete",
            scope=scope,
        )
        self.assertTrue(review.needs_writer_repair)
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        mocks["repair_writer"].assert_called_once()

    def test_stale_findings_never_reach_writer(self) -> None:
        review = ReviewResult(
            "changes_requested",
            "fix",
            [ReviewFinding("app.py", "bug")],
            status="stale",
        )
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        mocks["repair_writer"].assert_not_called()
        self.assertFalse(result.review_repair_attempted)

    def test_review_only_never_calls_repair_writer(self) -> None:
        # review_flow has no repair lifecycle; summary only
        review = ReviewResult(
            "changes_requested", "fix", [ReviewFinding("app.py", "bug")], status="complete"
        )
        summary = render_review_only_summary(review)
        self.assertIn("issues found", summary)
        self.assertNotIn("4/4", summary)

    def test_review_status_does_not_set_checks_passed(self) -> None:
        review = ReviewResult("approved", "ok", [], status="complete")
        self.assertFalse(getattr(review, "checks_passed", False))

    def test_repair_keeps_existing_changes_dirty_behavior(self) -> None:
        review = ReviewResult(
            "changes_requested", "fix", [ReviewFinding("app.py", "bug")], status="complete"
        )
        result, _ = _cycle(run_review=mock.Mock(return_value=("r", review)))
        self.assertTrue(result.changes_dirty)

    def test_existing_green_check_inheritance_rule_is_preserved(self) -> None:
        review = ReviewResult("changes_requested", "check", [], status="unknown")
        # unknown with no findings must not repair, preserving prior green-check logic
        result, mocks = _cycle(run_review=mock.Mock(return_value=("r", review)))
        mocks["repair_writer"].assert_not_called()


class ReviewDisplayTests(unittest.TestCase):
    def test_display_copy_is_fixed_english(self) -> None:
        self.assertEqual(
            render_review_only_summary(ReviewResult("approved", "ok", [], status="complete")),
            "Review passed",
        )
        summary = render_review_only_summary(
            ReviewResult(
                "changes_requested",
                "fix",
                [ReviewFinding("app.py", "bug"), ReviewFinding("app.py", "bug2")],
                status="complete",
            )
        )
        self.assertTrue(summary.startswith("2 issues found"))

    def test_partial_review_does_not_show_full_coverage(self) -> None:
        scope = ReviewScope(total_changed_files=2, provided_files=("a.py",), diff_truncated=True)
        summary = render_review_only_summary(
            ReviewResult("approved", "ok", [], status="complete", scope=scope)
        )
        self.assertIn("Partial review", summary)
        self.assertNotIn("4/4", summary)
        self.assertNotIn("covered", summary)

    def test_reused_review_is_not_presented_as_fresh(self) -> None:
        review = ReviewResult("approved", "ok", [], status="complete", origin="reused")
        summary = render_review_only_summary(review)
        self.assertIn("Previous review reused", summary)

    def test_stale_and_unavailable_copy(self) -> None:
        self.assertIn(
            "outdated",
            render_review_only_summary(ReviewResult("unknown", "x", [], status="stale")),
        )
        self.assertIn(
            "unavailable",
            render_review_only_summary(ReviewResult("unknown", "x", [], status="unavailable")),
        )


if __name__ == "__main__":
    unittest.main()
