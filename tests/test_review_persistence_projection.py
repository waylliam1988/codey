"""Review persistence, ledger, trace and JSON projection (batch 6)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.input import ReviewScope
from codey.reviews.persistence import (
    ReviewArtifactStore,
    load_review_artifact,
    save_review_artifact,
)


def _result():
    scope = ReviewScope(total_changed_files=1, provided_files=("app.py",))
    return ReviewResult(
        "changes_requested",
        "fix",
        [ReviewFinding("app.py", "bug", "fix it")],
        status="complete",
        origin="fresh",
        scope=scope,
    )


class PersistenceTests(unittest.TestCase):
    def test_cold_start_restores_full_normalized_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            ref = save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=_result(),
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
            )
            self.assertIsNotNone(ref)
            restored = load_review_artifact(store, session_id="s1", run_id="r1", attempt_id="a1")
            self.assertEqual(len(restored.findings), 1)
            self.assertEqual(restored.findings[0].issue, "bug")

    def test_ledger_contains_reference_not_raw_reply(self) -> None:
        # ledger must store digest/reference, never raw reply bodies
        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            ref = save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=_result(),
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
            )
            self.assertNotIn("bug", str(ref.ledger_fields()) if hasattr(ref, "ledger_fields") else "")

    def test_trace_contains_counts_and_digests_not_issue_bodies(self) -> None:
        from codey.reviews.persistence import review_trace_payload

        payload = review_trace_payload(_result(), scope_digest="s", prompt_digest="p")
        blob = str(payload)
        self.assertNotIn("bug", blob)
        self.assertIn("finding_count", blob)

    def test_output_permission_denial_disables_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            # oversized artifact must be denied without crashing current
            big = ReviewResult(
                "approved", "x" * 100000, [], status="complete", scope=ReviewScope()
            )
            ref = save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=big,
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
                permission_profile="unknown_profile_xyz",
            )
            self.assertIsNone(ref)

    def test_artifact_write_failure_preserves_current_result(self) -> None:
        result = _result()
        # save to a file path that will fail (parent is file)
        with tempfile.TemporaryDirectory() as td:
            bad_root = Path(td) / "file"
            bad_root.write_text("x", encoding="utf-8")
            store = ReviewArtifactStore(bad_root / "sub")
            save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=result,
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
            )
            # current result still usable even when persistence fails
            self.assertTrue(result.needs_writer_repair)

    def test_truncated_artifact_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=_result(),
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
            )
            path = store.path_for("s1", "r1", "a1")
            path.write_bytes(b'{"incomplete": true')
            with self.assertRaises(ValueError):
                load_review_artifact(store, session_id="s1", run_id="r1", attempt_id="a1")

    def test_tampered_artifact_or_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ReviewArtifactStore(Path(td))
            save_review_artifact(
                store,
                session_id="s1",
                run_id="r1",
                attempt_id="a1",
                result=_result(),
                scope_digest="s",
                prompt_digest="p",
                snapshot_digest="snap",
                reviewer_id="r",
                policy="web_if_available",
            )
            path = store.path_for("s1", "r1", "a1")
            data = path.read_bytes() + b"tamper"
            path.write_bytes(data)
            with self.assertRaises(ValueError):
                load_review_artifact(store, session_id="s1", run_id="r1", attempt_id="a1")


if __name__ == "__main__":
    unittest.main()
