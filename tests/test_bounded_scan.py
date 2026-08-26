from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.workspace.bounded_scan import (
    BoundedScanBudget,
    iter_bounded_files,
    iter_provided_files,
)


class BoundedScanTests(unittest.TestCase):
    def test_iter_bounded_files_maintains_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "a.py"
            second = root / "b.py"
            first.write_text("abc", encoding="utf-8")
            second.write_text("defgh", encoding="utf-8")
            budget = BoundedScanBudget(max_files=10, max_dirs=5, max_bytes=4)

            files = list(iter_bounded_files(root, excluded_dirs=set(), budget=budget))

        self.assertEqual([path.name for path in files], ["a.py"])
        self.assertEqual(budget.files_seen, 1)
        self.assertEqual(budget.bytes_seen, 3)
        self.assertTrue(budget.byte_limited)
        self.assertIn("byte budget 4", budget.stop_message("scan"))

    def test_iter_bounded_files_applies_byte_budget_to_start_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            start = root / "large.py"
            start.write_text("abcdef", encoding="utf-8")
            budget = BoundedScanBudget(max_files=10, max_dirs=5, max_bytes=4)

            files = list(iter_bounded_files(start, excluded_dirs=set(), budget=budget))

        self.assertEqual(files, [])
        self.assertEqual(budget.files_seen, 0)
        self.assertEqual(budget.bytes_seen, 0)
        self.assertTrue(budget.byte_limited)

    def test_iter_provided_files_maintains_total_byte_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "a.py"
            second = root / "b.py"
            first.write_text("abc", encoding="utf-8")
            second.write_text("defgh", encoding="utf-8")
            budget = BoundedScanBudget(max_files=10, max_bytes=4)

            files = list(iter_provided_files((first, second), budget))

        self.assertEqual([path.name for path in files], ["a.py"])
        self.assertEqual(budget.files_seen, 1)
        self.assertEqual(budget.bytes_seen, 3)
        self.assertTrue(budget.byte_limited)


if __name__ == "__main__":
    unittest.main()
