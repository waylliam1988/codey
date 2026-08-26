from __future__ import annotations

import unittest

from codey.runs.receipt import build_task_receipt


class TaskReceiptTests(unittest.TestCase):
    def test_no_changes_receipt_is_short(self) -> None:
        receipt = build_task_receipt({"mode": "snapshot", "changed_count": 0})

        self.assertEqual(receipt.text, "No files changed")
        self.assertEqual(receipt.changed_count, 0)
        self.assertFalse(receipt.restore_available)

    def test_snapshot_changes_include_restore(self) -> None:
        receipt = build_task_receipt(
            {"mode": "snapshot", "changed_count": 2},
            checks_passed=True,
        )

        self.assertEqual(receipt.text, "2 files changed · checks passed · restore available")
        self.assertEqual(receipt.changed_count, 2)
        self.assertTrue(receipt.checks_passed)
        self.assertTrue(receipt.restore_available)

    def test_git_changes_do_not_claim_snapshot_restore(self) -> None:
        receipt = build_task_receipt(
            {"mode": "git", "changed_count": 1},
            checks_passed=True,
        )

        self.assertEqual(receipt.text, "1 file changed · checks passed")
        self.assertFalse(receipt.restore_available)


if __name__ == "__main__":
    unittest.main()
