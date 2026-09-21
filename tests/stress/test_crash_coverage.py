"""The crash-point matrix keeps itself honest.

``checkpoints.py`` claims every durable writer x every crash point is
covered, vacuous, or same-by-construction. This test enforces the boring
parts of that claim: the table spans the full cross product, every
``covered`` cell names a test method that actually exists, and statuses
use only the known vocabulary. A new writer or crash point with no entry
fails here, not in a postmortem.
"""

from __future__ import annotations

import unittest

from tests.stress import checkpoints
from tests.stress import test_crash_point_matrix as _matrix
from tests.stress import test_proc_kill_recovery as _proc_kill

# Module aliases, never class imports: importing the TestCase classes here
# would make pytest collect (and run) their methods a second time.
_SUITES = (_matrix.CrashPointMatrixTests, _proc_kill.ProcKillRecoveryTests)


class CrashCoverageTests(unittest.TestCase):
    def test_matrix_spans_every_writer_and_point(self) -> None:
        self.assertEqual(
            set(checkpoints.COVERAGE),
            {
                (writer, point)
                for writer in checkpoints.WRITERS
                for point in checkpoints.CRASH_POINTS
            },
        )

    def test_covered_cells_name_real_tests(self) -> None:
        existing = set()
        for suite in _SUITES:
            existing.update(
                name for name in dir(suite) if name.startswith("test_")
            )
        missing = [
            (writer, point, test)
            for (writer, point), (status, test) in checkpoints.COVERAGE.items()
            if status == checkpoints.COVERED and test not in existing
        ]
        self.assertEqual(missing, [])

    def test_statuses_use_known_vocabulary(self) -> None:
        for cell, (status, detail) in checkpoints.COVERAGE.items():
            self.assertIn(status, (checkpoints.COVERED, checkpoints.VACUOUS, checkpoints.SAME))
            self.assertTrue(detail, f"{cell} needs a test name or a reason")

    def test_report_renders(self) -> None:
        report = checkpoints.coverage_report()
        self.assertIn("covered cells:", report)


if __name__ == "__main__":
    unittest.main()
