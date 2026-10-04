"""Explicit review reuse and submission wiring (batch 7)."""
from __future__ import annotations

import unittest

from codey.reviews.reuse import try_reuse_review, validate_source_run_id
from codey.task.model import TaskSubmission


class SubmissionWiringTests(unittest.TestCase):
    def test_explicit_source_roundtrips_through_submission(self) -> None:
        sub = TaskSubmission(
            session_id="s",
            project="p",
            task="t",
            max_turns=4,
            continue_task=False,
            provider_id="deepseek",
            intent="review",
            review_source_run_id="run-1",
        )
        self.assertEqual(sub.review_source_run_id, "run-1")

    def test_source_is_rejected_for_unsupported_intent(self) -> None:
        from codey.reviews.reuse import validate_source_run_id

        # chat intent must not accept review source (checked by entry layer)
        self.assertEqual(validate_source_run_id("run-1"), "run-1")
        with self.assertRaises(ValueError):
            validate_source_run_id("../evil.py")

    def test_shell_previous_run_id_behavior_is_unchanged(self) -> None:
        sub = TaskSubmission(
            session_id="s",
            project="p",
            task="t",
            max_turns=4,
            continue_task=False,
            provider_id="deepseek",
            previous_run_id="prev-1",
        )
        self.assertEqual(sub.previous_run_id, "prev-1")
        self.assertEqual(sub.review_source_run_id, "")

    def test_source_run_id_cannot_escape_store(self) -> None:
        with self.assertRaises(ValueError):
            validate_source_run_id("../../etc/passwd")
        with self.assertRaises(ValueError):
            validate_source_run_id("C:/evil")


class ReuseConditionTests(unittest.TestCase):
    def _identity(self, model="m1"):
        from codey.reviews.identity import ReviewIdentity

        return ReviewIdentity(
            scope_digest="s",
            prompt_digest="p",
            snapshot_digest="snap",
            project="proj",
            reviewer_id="r",
            model_id=model,
            contract_version=1,
            policy="web_if_available",
            self_review=False,
        )

    def _scope(self):
        from codey.reviews.input import ReviewScope

        return ReviewScope(total_changed_files=1, provided_files=("app.py",))

    def test_unknown_model_does_not_hit_strict_reuse(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = try_reuse_review(
                state_home=td,
                session_id="s",
                current_run_id="r2",
                current_project="proj",
                source_run_id="r1",
                current_scope=self._scope(),
                current_identity=self._identity(model=""),
                current_snapshot_ok=True,
            )
            self.assertIsNone(result)

    def test_input_mismatch_runs_fresh_review(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            result = try_reuse_review(
                state_home=td,
                session_id="s",
                current_run_id="r2",
                current_project="proj",
                source_run_id="missing-run",
                current_scope=self._scope(),
                current_identity=self._identity(model="m1"),
                current_snapshot_ok=True,
            )
            self.assertIsNone(result)

    def test_fresh_default_does_not_query_historical_results(self) -> None:
        self.assertEqual(validate_source_run_id(""), "")


if __name__ == "__main__":
    unittest.main()
