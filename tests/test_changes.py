from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codey.changes import ChangeTracker


class ChangeTrackerTests(unittest.TestCase):
    def test_collects_new_file_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tracker = ChangeTracker(root)

            tracker.capture_before("app.py")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            data = tracker.collect()

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "snapshot")
            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["status"], "A")
            self.assertEqual(data["files"][0]["additions"], 1)
            self.assertIn("diff --git a/app.py b/app.py", data["diff"])
            self.assertIn("--- /dev/null", data["diff"])
            self.assertIn("+++ b/app.py", data["diff"])
            self.assertIn("+print('ok')", data["diff"])

    def test_collects_modified_file_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\nsame\n", encoding="utf-8")
            tracker = ChangeTracker(root)

            tracker.capture_before("app.py")
            path.write_text("new\nsame\nextra\n", encoding="utf-8")
            data = tracker.collect()

            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["status"], "M")
            self.assertIn("diff --git a/app.py b/app.py", data["diff"])
            self.assertIn("-old", data["diff"])
            self.assertIn("+new", data["diff"])
            self.assertIn("+extra", data["diff"])

    def test_restore_reverts_modified_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")

            result = tracker.restore()

            self.assertTrue(result.ok, result)
            self.assertEqual(result.restored, ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual(tracker.collect()["changed_count"], 0)

    def test_restore_removes_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")

            result = tracker.restore()

            self.assertTrue(result.ok, result)
            self.assertFalse(path.exists())

    def test_restore_conflicts_when_file_changed_after_snapshot_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.collect()
            path.write_text("manual\n", encoding="utf-8")

            result = tracker.restore()

            self.assertFalse(result.ok)
            self.assertEqual(result.conflicts, ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "manual\n")

    def test_capture_rejects_paths_outside_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tracker = ChangeTracker(td)

            with self.assertRaisesRegex(ValueError, "escapes project root"):
                tracker.capture_before("../escape.py")


if __name__ == "__main__":
    unittest.main()
