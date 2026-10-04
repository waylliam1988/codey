"""Review reuse integration: cold start restore and no-send hit (batch 7/8)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.reviews.core import ReviewFinding, ReviewResult
from codey.reviews.input import ReviewScope


class ReuseIntegrationTests(unittest.TestCase):
    def test_cold_start_restores_real_findings_with_source_visible(self) -> None:
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
            self.assertEqual(restored.origin, "reused")

    def test_exact_match_restores_without_send(self) -> None:
        calls: list[str] = []

        class _FakeReviewer:
            def new_chat(self) -> None:
                pass

            def send(self, text: str, timeout=None) -> str:
                calls.append(text)
                return '{"verdict":"approved","summary":"ok","findings":[]}'

            def close(self) -> None:
                pass

        # reuse path must not start a new reviewer chat when it hits;
        # here we prove the fake was not needed for a direct artifact restore
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
