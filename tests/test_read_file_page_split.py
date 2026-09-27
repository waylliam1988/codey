"""read_file read/format boundary split locks (red-first).

Only `read_file` gets a small split: the 811-870 page-formatting block
becomes a helper taking (rel, start_line, line_limit, total, lines),
while the streaming read loop keeps its cancellation checks.
The other six 19-20 complexity functions stay as-is; the gate stays at 20.
These tests assert the SPLIT state, so structure asserts fail before.
"""
from __future__ import annotations

import inspect as std_inspect
import tempfile
import unittest
from pathlib import Path


class ReadFileSplitStructureTests(unittest.TestCase):
    def test_format_helper_exists_with_page_boundary_signature(self) -> None:
        import codey.toolchain.runtime as rt

        self.assertTrue(
            hasattr(rt, "_format_read_file_page"),
            "_format_read_file_page should be extracted",
        )
        sig = std_inspect.signature(rt._format_read_file_page)
        self.assertEqual(
            list(sig.parameters),
            ["rel", "start_line", "line_limit", "total", "lines"],
        )

    def test_read_file_delegates_and_keeps_read_loop_checks(self) -> None:
        import codey.toolchain.runtime as rt

        read_source = std_inspect.getsource(rt.read_file)
        helper_source = std_inspect.getsource(rt._format_read_file_page)
        # Delegation: read_file calls the helper exactly once.
        self.assertIn("_format_read_file_page(", read_source)
        self.assertEqual(read_source.count("_format_read_file_page("), 1)
        # Formatting bulk lives in the helper, not in read_file.
        for marker in (
            "preview only, not a complete old_string",
            "read_file page: lines",
            "read_file page: line",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, read_source)
                self.assertIn(marker, helper_source)
        # Read loop keeps its periodic cancellation check.
        self.assertIn("cancellation.check()", read_source)
        self.assertIn("total % 500", read_source)

    def test_complexity_gate_stays_at_20(self) -> None:
        import tomllib

        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"], 20)
        self.assertEqual(data["tool"]["ruff"]["lint"]["pylint"]["max-branches"], 20)
        self.assertEqual(data["tool"]["ruff"]["lint"]["pylint"]["max-statements"], 80)


class ReadFileSplitBehaviorTests(unittest.TestCase):
    def test_paging_and_metadata_unchanged(self) -> None:
        from codey.toolchain.runtime import read_file

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.txt").write_text(
                "one\ntwo\nthree\nfour\nfive\n", encoding="utf-8"
            )
            first = read_file(root, "app.txt", limit=2)
            last = read_file(root, "app.txt", offset=5, limit=2)

        self.assertTrue(first.truncated)
        self.assertTrue(first.model_text.startswith("one\ntwo\n"))
        self.assertIn("lines 1-2 of 5; next offset=3", first.model_text)
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"app.txt","offset":3,"limit":2}}',
            first.model_text,
        )
        self.assertTrue(last.model_text.startswith("five\n"))
        self.assertIn("lines 5-5 of 5", last.model_text)
        self.assertNotIn("next offset=", last.model_text)

    def test_overlong_first_line_preview_unchanged(self) -> None:
        from codey.toolchain.runtime import (
            LONG_LINE_MARKER,
            READ_MAX_CHARS,
            read_file,
        )

        line = "HEAD" + ("x" * READ_MAX_CHARS) + "TAIL\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "generated.txt").write_text(line + "next\n", encoding="utf-8")
            outcome = read_file(root, "generated.txt")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("HEAD", outcome.model_text)
        self.assertIn("TAIL", outcome.model_text)
        self.assertIn(LONG_LINE_MARKER.strip(), outcome.model_text)
        self.assertIn("preview only, not a complete old_string", outcome.model_text)
        self.assertIn("next offset=2", outcome.model_text)

    def test_empty_and_bounds_unchanged(self) -> None:
        from codey.toolchain.runtime import read_file

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "empty.txt").write_text("", encoding="utf-8")
            (root / "one.txt").write_text("one\n", encoding="utf-8")
            empty = read_file(root, "empty.txt")
            past_end = read_file(root, "one.txt", offset=2)
            full = read_file(root, "one.txt")

        self.assertEqual(empty.model_text, "")
        self.assertTrue(empty.ok)
        self.assertFalse(past_end.ok)
        self.assertIn("exceeds one.txt total lines 1", past_end.model_text)
        self.assertTrue(full.ok)
        self.assertFalse(full.truncated)
        self.assertEqual(full.model_text, "one\n")


if __name__ == "__main__":
    unittest.main()
