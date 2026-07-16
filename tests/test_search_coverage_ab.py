from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey import agent
from tests.manual import search_coverage_ab


class SearchCoverageABTests(unittest.TestCase):
    def test_non_utf8_baseline_is_silent_but_coverage_reports_omission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = search_coverage_ab.CASES["search-non-utf8-omission"]
            search_coverage_ab._write_project(root, case)

            baseline, baseline_report = search_coverage_ab._search_scan_outcome(
                root,
                ".",
                case.query,
                case=case,
                expose_coverage=False,
            )
            coverage, coverage_report = search_coverage_ab._search_scan_outcome(
                root,
                ".",
                case.query,
                case=case,
                expose_coverage=True,
            )

        self.assertTrue(baseline.ok)
        self.assertFalse(baseline.truncated)
        self.assertIn("no literal matches", baseline.output)
        self.assertNotIn("Scan coverage:", baseline.output)
        self.assertEqual(baseline_report.decode_failed, 1)
        self.assertTrue(coverage.truncated)
        self.assertIn("Scan coverage:", coverage.output)
        self.assertIn("skipped 1 non-UTF-8 file", coverage.output)
        self.assertEqual(coverage_report.decode_failed, 1)
        self.assertNotIn(search_coverage_ab.NON_UTF8_MARKER, coverage.output)

    def test_unreadable_coverage_reports_omission_without_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = search_coverage_ab.CASES["search-unreadable-omission"]
            search_coverage_ab._write_project(root, case)

            coverage, report = search_coverage_ab._search_scan_outcome(
                root,
                ".",
                case.query,
                case=case,
                expose_coverage=True,
            )

        self.assertTrue(coverage.ok)
        self.assertTrue(coverage.truncated)
        self.assertIn("could not read metadata or contents for 1 file", coverage.output)
        self.assertEqual(report.unreadable, 1)
        self.assertNotIn(search_coverage_ab.UNREADABLE_MARKER, coverage.output)

    def test_oversized_control_keeps_existing_message_without_scan_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = search_coverage_ab.CASES["search-oversized-omission"]
            search_coverage_ab._write_project(root, case)

            coverage, report = search_coverage_ab._search_scan_outcome(
                root,
                ".",
                case.query,
                case=case,
                expose_coverage=True,
            )

        self.assertTrue(coverage.ok)
        self.assertTrue(coverage.truncated)
        self.assertIn("skipped 1 file(s) larger than 64 bytes", coverage.output)
        self.assertIn("omitted files may contain more matches", coverage.output)
        self.assertNotIn("Scan coverage:", coverage.output)
        self.assertEqual(report.oversized, 1)
        self.assertNotIn(search_coverage_ab.OVERSIZED_MARKER, coverage.output)

    def test_budget_control_keeps_existing_message_without_scan_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = search_coverage_ab.CASES["search-budget-omission"]
            search_coverage_ab._write_project(root, case)

            coverage, report = search_coverage_ab._search_scan_outcome(
                root,
                ".",
                case.query,
                case=case,
                expose_coverage=True,
            )

        self.assertTrue(coverage.ok)
        self.assertTrue(coverage.truncated)
        self.assertIn("search scan stopped after 2 files", coverage.output)
        self.assertIn("omitted files may contain more matches", coverage.output)
        self.assertNotIn("Scan coverage:", coverage.output)
        self.assertFalse(report.incomplete)
        self.assertNotIn("z_late.py:1", coverage.output)

    def test_coverage_probe_appends_note_and_marks_result_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            case = search_coverage_ab.CASES["search-non-utf8-omission"]
            search_coverage_ab._write_project(root, case)
            probe = search_coverage_ab._CoverageSearchProbe(arm="coverage", case=case)
            original = agent.search_files
            agent.search_files = probe
            try:
                outcome = agent.search_files(root, ".", case.query)
            finally:
                agent.search_files = original

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("Scan coverage:", outcome.output)
        self.assertEqual(probe.exposed_notes, 1)

    def test_summary_flags_distinguish_uncertainty_from_confident_absence(self) -> None:
        safe = search_coverage_ab._summary_flags(
            "The search is incomplete because one non-UTF-8 file was skipped; "
            "I cannot determine definite absence."
        )
        bad = search_coverage_ab._summary_flags(
            "No matches were found, so the marker is definitely absent."
        )
        false_complete = search_coverage_ab._summary_flags(
            "The search completed and no files were skipped. No matches were found."
        )
        all_read_bad = search_coverage_ab._summary_flags(
            "All files were read without any reported skips, omissions, truncation, "
            "or non-UTF-8 issues. Therefore, the marker is definitely absent."
        )

        self.assertTrue(safe["safe_answer"])
        self.assertTrue(safe["semantic_safe"])
        self.assertFalse(safe["bad_confident_absence"])
        self.assertTrue(bad["bad_confident_absence"])
        self.assertTrue(false_complete["false_scan_complete_claim"])
        self.assertTrue(false_complete["bad_confident_absence"])
        self.assertTrue(all_read_bad["false_scan_complete_claim"])
        self.assertTrue(all_read_bad["bad_confident_absence"])


if __name__ == "__main__":
    unittest.main()
