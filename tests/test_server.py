from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from codey import server


class GitChangesTests(unittest.TestCase):
    def test_parse_git_status(self) -> None:
        files = server.parse_git_status(" M codey/server.py\n?? new.txt\nR  old.py -> new.py\n")

        self.assertEqual(files[0]["status"], "M")
        self.assertEqual(files[0]["path"], "codey/server.py")
        self.assertEqual(files[1]["status"], "??")
        self.assertEqual(files[1]["path"], "new.txt")
        self.assertEqual(files[2]["status"], "R")
        self.assertEqual(files[2]["path"], "old.py -> new.py")

    def test_collect_git_changes_rejects_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = server.collect_git_changes(td)

            self.assertFalse(data["ok"])
            self.assertEqual(data["error"], "not a git repository")

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_collect_git_changes_includes_untracked_text_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            data = server.collect_git_changes(root)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["path"], "note.txt")
            self.assertEqual(data["files"][0]["status"], "??")
            self.assertEqual(data["files"][0]["additions"], 2)
            self.assertIn("+++ b/note.txt", data["diff"])
            self.assertIn("+hello", data["diff"])


if __name__ == "__main__":
    unittest.main()
