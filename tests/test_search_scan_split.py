"""Behavior locks for search/scan helpers (pure-extraction safety net).

Covers the four refactored surfaces only: ``search_files``,
``iter_bounded_files``, ``collect_git_changes`` and ``find_reference_hints``.
Includes one deterministic bug probe: a caller-budgeted symlink must be
skipped, never followed/duplicated.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.toolchain import runtime as tool_runtime
from codey.utils.references import find_reference_hints
from codey.workspace.bounded_scan import BoundedScanBudget, iter_bounded_files
from codey.workspace.changes import collect_git_changes


def _try_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
    except OSError:
        return False
    return True


class SearchFilesBehaviorTests(unittest.TestCase):
    def test_literal_match_and_no_match_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "a.py").write_text("hello needle here\nnothing\n", encoding="utf-8")

            hit = tool_runtime.search_files(root, ".", "needle")
            miss = tool_runtime.search_files(root, ".", "absent-token-xyz")

        self.assertTrue(hit.ok)
        self.assertFalse(hit.truncated)
        self.assertIn("a.py:1: hello needle here", hit.model_text)
        self.assertIn("(no literal matches; regex is not supported)", miss.model_text)

    def test_pagination_next_offset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            body = "".join(f"match line {index}\n" for index in range(5))
            Path(root, "a.py").write_text(body, encoding="utf-8")

            page1 = tool_runtime.search_files(root, ".", "match", offset=1, limit=2)
            page2 = tool_runtime.search_files(root, ".", "match", offset=3, limit=2)

        self.assertTrue(page1.truncated)
        self.assertIn("a.py:1: match line 0", page1.model_text)
        self.assertIn("a.py:2: match line 1", page1.model_text)
        self.assertNotIn("a.py:3:", page1.model_text)
        self.assertIn("next offset=3", page1.model_text)
        self.assertIn("a.py:3: match line 2", page2.model_text)
        self.assertIn("a.py:4: match line 3", page2.model_text)

    def test_oversized_counter_and_footer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "big.py").write_text("needle " * 64, encoding="utf-8")

            with mock.patch.object(tool_runtime, "SEARCH_MAX_FILE_BYTES", 10):
                outcome = tool_runtime.search_files(root, ".", "needle")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("skipped 1 file(s) larger than 10 bytes", outcome.model_text)

    def test_symlink_skipped_silently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.py"
            target.write_text("needle here\n", encoding="utf-8")
            if not _try_symlink(root / "link.py", target):
                self.skipTest("symlink creation unavailable")

            outcome = tool_runtime.search_files(root, ".", "needle")

        self.assertIn("target.py:1: needle here", outcome.model_text)
        self.assertNotIn("link.py", outcome.model_text)
        self.assertNotIn("could not read metadata", outcome.model_text)
        self.assertFalse(outcome.truncated)


class BoundedScanBehaviorTests(unittest.TestCase):
    def test_excluded_dirs_skipped_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "b.py").write_text("x", encoding="utf-8")
            Path(root, "a.py").write_text("x", encoding="utf-8")
            sub = root / "sub"
            sub.mkdir()
            Path(sub, "c.py").write_text("x", encoding="utf-8")
            skipped = root / "node_modules"
            skipped.mkdir()
            Path(skipped, "skip.py").write_text("x", encoding="utf-8")

            budget = BoundedScanBudget()
            got = [
                path.relative_to(root).as_posix()
                for path in iter_bounded_files(root, excluded_dirs={"node_modules"}, budget=budget)
            ]

        self.assertEqual(got, ["a.py", "b.py", "sub/c.py"])
        self.assertFalse(budget.limited)

    def test_dir_budget_stops_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "a.py").write_text("x", encoding="utf-8")
            sub = root / "sub"
            sub.mkdir()
            Path(sub, "c.py").write_text("x", encoding="utf-8")

            budget = BoundedScanBudget(max_dirs=1)
            got = [
                path.relative_to(root).as_posix()
                for path in iter_bounded_files(root, excluded_dirs=set(), budget=budget)
            ]

        self.assertEqual(got, ["a.py"])
        self.assertTrue(budget.dir_limited)


class CollectGitChangesBehaviorTests(unittest.TestCase):
    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )

    def test_non_repo_reason(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = collect_git_changes(td)

        self.assertFalse(data["ok"])
        self.assertEqual(data.get("reason"), "not_repo")
        self.assertEqual(data["files"], [])

    def test_modified_and_untracked_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._git(repo, "init")
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "test")
            Path(repo, "tracked.txt").write_text("a\n", encoding="utf-8")
            self._git(repo, "add", "tracked.txt")
            self._git(repo, "commit", "-m", "init")
            Path(repo, "tracked.txt").write_text("a\nb\n", encoding="utf-8")
            Path(repo, "new.txt").write_text("x\n", encoding="utf-8")

            data = collect_git_changes(repo)

        self.assertTrue(data["ok"])
        self.assertEqual(data["changed_count"], 2)
        by_path = {item["path"]: item for item in data["files"]}
        self.assertEqual(by_path["new.txt"]["status"], "??")
        self.assertEqual(by_path["new.txt"]["additions"], 1)
        self.assertIn("new.txt", data["diff"])


class ReferenceHintsBehaviorTests(unittest.TestCase):
    def test_classify_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "a.py").write_text(
                "from m import my_symbol\n"
                "def my_symbol():\n"
                "result = my_symbol()\n"
                "x = my_symbol\n",
                encoding="utf-8",
            )

            scan = find_reference_hints(root, root, "my_symbol")

        self.assertFalse(scan.truncated)
        self.assertIn("- import a.py:1: from m import my_symbol", scan.output)
        self.assertIn("- definition a.py:2: def my_symbol():", scan.output)
        self.assertIn("- call a.py:3: result = my_symbol()", scan.output)
        self.assertIn("- reference a.py:4: x = my_symbol", scan.output)

    def test_max_results_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "a.py").write_text("my_symbol\nmy_symbol\nmy_symbol\n", encoding="utf-8")

            scan = find_reference_hints(root, root, "my_symbol", max_results=2)

        self.assertTrue(scan.truncated)
        self.assertIn("- references truncated after 2 matches", scan.output)

    def test_budgeted_symlink_file_skipped_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.py"
            target.write_text("MY_SYMBOL = 1\nprint(MY_SYMBOL)\n", encoding="utf-8")
            if not _try_symlink(root / "link.py", target):
                self.skipTest("symlink creation unavailable")

            scan = find_reference_hints(
                root,
                root,
                "MY_SYMBOL",
                files=[root / "link.py", target],
                files_budgeted=True,
                scan_budget=BoundedScanBudget(),
            )

        rows = [line for line in scan.output.splitlines() if ".py:" in line]
        self.assertEqual(
            rows,
            [
                "- reference target.py:1: MY_SYMBOL = 1",
                "- reference target.py:2: print(MY_SYMBOL)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
