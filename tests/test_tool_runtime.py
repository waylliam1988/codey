from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation
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

    def test_run_command_is_cancelled_without_waiting_for_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sleep.py").write_text(
                "import time\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            event = threading.Event()
            timer = threading.Timer(0.1, event.set)
            started = time.monotonic()
            timer.start()
            try:
                with cancellation.scope(event):
                    with self.assertRaises(cancellation.TaskCancelled):
                        run_command(root, ".", "python sleep.py")
            finally:
                timer.cancel()

        self.assertLess(time.monotonic() - started, 2.0)

    def test_run_command_preserves_head_and_tail_of_large_output(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            ["python", "large.py"],
            1,
            stdout="HEAD" + ("x" * 200) + "TAIL",
            stderr="",
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch("codey.tool_runtime.RUN_OUTPUT_LIMIT", 80),
            mock.patch("codey.tool_runtime.cancellation.run_process", return_value=completed),
        ):
            outcome = run_command(Path(td), ".", "python large.py")

        self.assertTrue(outcome.truncated)
        self.assertIn("HEAD", outcome.output)
        self.assertTrue(outcome.output.endswith("TAIL"))
        self.assertIn("middle of output omitted", outcome.output)

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
