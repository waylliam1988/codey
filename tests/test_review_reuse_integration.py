"""Review reuse integration: cold start restore and no-send hit (batch 7/8)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.input import ReviewScope


class ReuseIntegrationTests(unittest.TestCase):
    def test_cold_start_restores_real_fresh_findings(self) -> None:
        from codey.reviews.persistence import (
            ReviewArtifactStore,
            load_review_artifact,
            save_review_artifact,
        )

        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            result = ReviewResult(
                "changes_requested", "fix",
                [ReviewFinding("app.py", "bug", "fix it")],
                status="complete", scope=scope,
            )
            ref = save_review_artifact(
                store, session_id="s", run_id="r1", attempt_id="a1",
                result=result, scope_digest="s", prompt_digest="p",
                snapshot_digest="snap", reviewer_id="r", policy="web_if_available",
                model_id="m1",
            )
            self.assertIsNotNone(ref)
            # destroy memory: reload from disk only
            restored = load_review_artifact(store, session_id="s", run_id="r1", attempt_id="a1")
            self.assertEqual(restored.findings[0].issue, "bug")
            self.assertEqual(restored.origin, "fresh")

    def test_exact_match_restores_result_from_finished_source(self) -> None:
        from codey.reviews.identity import ReviewIdentity
        from codey.reviews.reuse import try_reuse_review
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            result = ReviewResult(
                "approved", "ok", [], status="complete", scope=scope,
            )
            from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact

            ref = save_review_artifact(
                ReviewArtifactStore(state),
                session_id="s", run_id="r1", attempt_id="a1", result=result,
                scope_digest="scope", prompt_digest="prompt", snapshot_digest="snap",
                reviewer_id="r", policy="web_if_available", model_id="r",
            )
            self.assertIsNotNone(ref)
            ledger = RunLedgerStore(state).open(
                run_id="r1", session_id="s", project="p", task="t",
                provider="r", mode="review",
            )
            for event in ("review_result_projected", "review_finished"):
                ledger.append(
                    event, review_attempt_id="a1", verdict="approved",
                    status="complete", origin="fresh", finding_count=0,
                    artifact_sha256=ref.sha256,
                )
            ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="r")
            identity = ReviewIdentity(
                scope_digest="scope", prompt_digest="prompt", snapshot_digest="snap",
                project="p", reviewer_id="r", model_id="r", contract_version=1,
                policy="web_if_available", self_review=False,
            )
            restored = try_reuse_review(
                state_home=state, session_id="s", current_run_id="r2",
                current_project="p", source_run_id="r1", current_scope=scope,
                current_identity=identity, current_snapshot_ok=True,
            )

        self.assertIsNotNone(restored)
        self.assertEqual(restored.origin, "reused")

    def test_missing_current_project_does_not_bypass_project_binding(self) -> None:
        from codey.reviews.identity import ReviewIdentity
        from codey.reviews.persistence import ReviewArtifactStore, save_review_artifact
        from codey.reviews.reuse import try_reuse_review
        from codey.runs.ledger import RunLedgerStore

        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
            ref = save_review_artifact(
                ReviewArtifactStore(state),
                session_id="s", run_id="r1", attempt_id="a1",
                result=ReviewResult("approved", "ok", [], status="complete", scope=scope),
                scope_digest="scope", prompt_digest="prompt", snapshot_digest="snap",
                reviewer_id="r", policy="web_if_available", model_id="r",
            )
            assert ref is not None
            ledger = RunLedgerStore(state).open(
                run_id="r1", session_id="s", project="project-a", task="t",
                provider="r", mode="review",
            )
            for event in ("review_result_projected", "review_finished"):
                ledger.append(
                    event, review_attempt_id="a1", verdict="approved",
                    status="complete", origin="fresh", finding_count=0,
                    artifact_sha256=ref.sha256,
                )
            ledger.finish(summary="done", stop_reason="done", turns=1, max_turns=1, provider="r")
            reused = try_reuse_review(
                state_home=state, session_id="s", current_run_id="r2",
                current_project="", source_run_id="r1", current_scope=scope,
                current_identity=ReviewIdentity(
                    scope_digest="scope", prompt_digest="prompt", snapshot_digest="snap",
                    project="", reviewer_id="r", model_id="r", contract_version=1,
                    policy="web_if_available", self_review=False,
                ),
                current_snapshot_ok=True,
            )
        self.assertIsNone(reused)


if __name__ == "__main__":
    unittest.main()
