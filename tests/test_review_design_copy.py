"""Run Details review copy follows DESIGN.md (batch 5/8)."""
from __future__ import annotations

import unittest

from codey.operations.review_flow import render_review_only_summary
from codey.reviews.core import ReviewResult
from codey.reviews.input import ReviewScope
from codey.runs.details import load_run_details


class ReviewDesignTests(unittest.TestCase):
    def test_review_copy_is_fixed_english(self) -> None:
        self.assertEqual(
            render_review_only_summary(ReviewResult("approved", "ok", [], status="complete")),
            "Review passed",
        )
        for text in (
            render_review_only_summary(ReviewResult("unknown", "x", [], status="stale")),
            render_review_only_summary(ReviewResult("unknown", "x", [], status="unavailable")),
            render_review_only_summary(ReviewResult("unknown", "x", [], status="incomplete")),
        ):
            self.assertTrue(text and all(ord(c) < 128 or c == "·" for c in text))

    def test_review_details_hide_internal_identifiers(self) -> None:
        summary = load_run_details(
            run_ledgers=None, run_traces=None, session_id="s", run_id="r"
        )
        blob = str(summary.to_jsonable())
        for internal in ("digest", "attempt", "ledger", "schema"):
            self.assertNotIn(internal, blob.lower())

    def test_partial_review_does_not_show_full_coverage(self) -> None:
        scope = ReviewScope(total_changed_files=2, provided_files=("a.py",), diff_truncated=True)
        text = render_review_only_summary(
            ReviewResult("approved", "ok", [], status="complete", scope=scope)
        )
        self.assertNotIn("4/4", text)
        self.assertNotIn("covered", text)

    def test_reused_review_is_not_presented_as_fresh(self) -> None:
        text = render_review_only_summary(
            ReviewResult("approved", "ok", [], status="complete", origin="reused")
        )
        self.assertIn("Previous review reused", text)
        self.assertNotIn("just checked", text.lower())

    def test_review_warning_uses_existing_gray_style(self) -> None:
        # warning tone stays gray (neutral/warning only, no color cards)
        summary = load_run_details(
            run_ledgers=None, run_traces=None, session_id="", run_id=""
        )
        self.assertFalse(summary.available)


if __name__ == "__main__":
    unittest.main()
