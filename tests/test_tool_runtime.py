from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.tool_runtime import edit_file, read_file, run_command, write_file


class ToolOutcomeTests(unittest.TestCase):
    def test_file_tools_report_structured_success_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            written = write_file(root, "result.txt", "ok")
            read = read_file(root, "result.txt")

        self.assertTrue(written.ok)
        self.assertTrue(written.changed)
        self.assertIsNone(written.exit_code)
        self.assertTrue(read.ok)
        self.assertEqual(read.output, "ok")

    def test_failed_check_has_exit_code_without_becoming_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fail.py").write_text("raise SystemExit(3)\n", encoding="utf-8")

            outcome = run_command(root, ".", "python fail.py")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 3)
        self.assertTrue(outcome.output.startswith("exit 3:"))
        self.assertFalse(outcome.output.startswith("ERROR:"))

    def test_writing_identical_content_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "same.txt"
            path.write_text("same", encoding="utf-8")

            outcome = write_file(root, "same.txt", "same")

        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertIn("no changes", outcome.output)

    def test_creating_empty_file_is_still_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outcome = write_file(Path(td), "empty.txt", "")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.changed)

    def test_write_and_edit_reject_oversized_result_without_changing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.txt"
            path.write_text("old", encoding="utf-8")
            body = "<<<<<<< SEARCH\nold\n=======\nlarge\n>>>>>>> REPLACE"

            with mock.patch("codey.tool_runtime.WRITE_MAX_FILE_BYTES", 4):
                written = write_file(root, "new.txt", "large")
                edited = edit_file(root, "app.txt", body)

            self.assertFalse(written.ok)
            self.assertFalse((root / "new.txt").exists())
            self.assertFalse(edited.ok)
            self.assertEqual(path.read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
