from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation
from codey.references import find_reference_hints
from codey.tool_runtime import (
    EditBlock,
    LONG_LINE_MARKER,
    MAX_REPLACEMENTS,
    READ_MAX_CHARS,
    READ_MAX_LINES,
    edit_file,
    find_references,
    outline_file,
    read_file,
    run_command,
    write_file,
)


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

    def test_read_file_pages_on_complete_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.txt").write_text(
                "one\ntwo\nthree\nfour\nfive\n",
                encoding="utf-8",
            )

            first = read_file(root, "app.txt", limit=2)
            middle = read_file(root, "app.txt", offset=3, limit=2)
            last = read_file(root, "app.txt", offset=5, limit=2)

        self.assertTrue(first.truncated)
        self.assertTrue(first.output.startswith("one\ntwo\n"))
        self.assertIn("lines 1-2 of 5; next offset=3", first.output)
        self.assertTrue(middle.output.startswith("three\nfour\n"))
        self.assertIn("next offset=5", middle.output)
        self.assertTrue(last.output.startswith("five\n"))
        self.assertIn("lines 5-5 of 5", last.output)
        self.assertNotIn("next offset=", last.output)

    def test_read_file_character_budget_never_splits_a_normal_line(self) -> None:
        first_line = "a" * (READ_MAX_CHARS // 2) + "\n"
        second_line = "b" * (READ_MAX_CHARS // 2) + "\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "large.txt").write_text(first_line + second_line, encoding="utf-8")

            outcome = read_file(root, "large.txt")

        content, metadata = outcome.output.rsplit("\n\n[", 1)
        self.assertEqual(content, first_line.rstrip("\n"))
        self.assertNotIn("b", content)
        self.assertIn("lines 1-1 of 2; next offset=2", metadata)

    def test_read_file_marks_overlong_line_as_preview_only(self) -> None:
        line = "HEAD" + ("x" * READ_MAX_CHARS) + "TAIL\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "generated.txt").write_text(line + "next\n", encoding="utf-8")

            outcome = read_file(root, "generated.txt")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("HEAD", outcome.output)
        self.assertIn("TAIL", outcome.output)
        self.assertIn(LONG_LINE_MARKER.strip(), outcome.output)
        self.assertIn("preview only, not a complete old_string", outcome.output)
        self.assertIn("next offset=2", outcome.output)

    def test_read_file_validates_page_bounds_and_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "empty.txt").write_text("", encoding="utf-8")
            (root / "one.txt").write_text("one\n", encoding="utf-8")

            empty = read_file(root, "empty.txt")
            past_end = read_file(root, "one.txt", offset=2)
            zero = read_file(root, "one.txt", limit=0)
            fractional = read_file(root, "one.txt", offset=1.5)
            oversized = read_file(root, "one.txt", limit=READ_MAX_LINES + 1)

        self.assertEqual(empty.output, "")
        self.assertFalse(past_end.ok)
        self.assertIn("exceeds one.txt total lines 1", past_end.output)
        self.assertIn("positive integer", zero.output)
        self.assertIn("positive integer", fractional.output)
        self.assertIn(f"at most {READ_MAX_LINES}", oversized.output)

    def test_outline_file_returns_python_navigation_without_body_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "app.py").write_text(
                "import os\n"
                "from pathlib import Path\n\n"
                "def helper(value):\n"
                "    return 'BODY_SECRET'\n\n"
                "class Service:\n"
                "    def handle(self, request):\n"
                "        return request\n\n"
                "def test_service():\n"
                "    assert True\n",
                encoding="utf-8",
            )

            outcome = outline_file(root, "app.py")

        self.assertTrue(outcome.ok)
        self.assertIn("File outline: app.py", outcome.output)
        self.assertIn("import os line 1", outcome.output)
        self.assertIn("function helper(value) line 4", outcome.output)
        self.assertIn("class Service line 7", outcome.output)
        self.assertIn("method Service.handle(self, request) line 8", outcome.output)
        self.assertIn("function test_service() line 11", outcome.output)
        self.assertIn("use read_file before editing", outcome.output)
        self.assertNotIn("BODY_SECRET", outcome.output)

    def test_outline_file_returns_typescript_routes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "src").mkdir()
            Path(root, "src", "router.ts").write_text(
                "import express from 'express';\n"
                "const router = express.Router();\n"
                "export function createRouter() {\n"
                "  router.get('/health', handler);\n"
                "  app.post('/login', loginHandler);\n"
                "}\n"
                "const makeSession = () => ({ ok: true });\n",
                encoding="utf-8",
            )

            outcome = outline_file(root, "src/router.ts")

        self.assertTrue(outcome.ok)
        self.assertIn("import express line 1", outcome.output)
        self.assertIn("export createRouter line 3", outcome.output)
        self.assertIn("route router.GET /health line 4", outcome.output)
        self.assertIn("route app.POST /login line 5", outcome.output)
        self.assertIn("arrow makeSession line 7", outcome.output)
        self.assertNotIn("ok: true", outcome.output)

    def test_outline_file_marks_javascript_truncated_only_after_limit_is_exceeded(self) -> None:
        exact_limit = "\n".join(
            f"function item{index}() {{ return {index}; }}"
            for index in range(80)
        )
        over_limit = exact_limit + "\nfunction item80() { return 80; }\n"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "exact.js").write_text(exact_limit, encoding="utf-8")
            Path(root, "over.js").write_text(over_limit, encoding="utf-8")

            exact = outline_file(root, "exact.js")
            over = outline_file(root, "over.js")

        self.assertTrue(exact.ok)
        self.assertFalse(exact.truncated)
        self.assertNotIn("outline truncated", exact.output)
        self.assertIn("function item79 line 80", exact.output)
        self.assertTrue(over.ok)
        self.assertTrue(over.truncated)
        self.assertIn("outline truncated", over.output)
        self.assertNotIn("item80", over.output)

    def test_outline_file_handles_unsupported_large_and_non_utf8_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "notes.txt").write_text("hello\n", encoding="utf-8")
            Path(root, "bad.py").write_bytes(b"\xff\xfe")
            Path(root, "large.py").write_text("x" * 32, encoding="utf-8")

            unsupported = outline_file(root, "notes.txt")
            non_utf8 = outline_file(root, "bad.py")
            with mock.patch("codey.tool_runtime.OUTLINE_MAX_FILE_BYTES", 8):
                large = outline_file(root, "large.py")

        self.assertTrue(unsupported.ok)
        self.assertIn("outline unavailable", unsupported.output)
        self.assertFalse(non_utf8.ok)
        self.assertIn("not utf-8 text", non_utf8.output)
        self.assertFalse(large.ok)
        self.assertIn("file too large to outline", large.output)

    def test_find_references_returns_python_reference_hints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "pricing.py").write_text(
                "def calculate_total(amount, tax_rate):\n"
                "    return amount * (1 + tax_rate)\n\n"
                "class calculate_totalMock:\n"
                "    pass\n",
                encoding="utf-8",
            )
            Path(root, "checkout.py").write_text(
                "from pricing import calculate_total\n\n"
                "def checkout():\n"
                "    return calculate_total(100, 0.2)\n",
                encoding="utf-8",
            )

            outcome = find_references(root, ".", "calculate_total")

        self.assertTrue(outcome.ok)
        self.assertIn("References for calculate_total under .", outcome.output)
        self.assertIn("lexical scan, not semantic resolution", outcome.output)
        self.assertIn("definition pricing.py:1: def calculate_total", outcome.output)
        self.assertIn("import checkout.py:1: from pricing import calculate_total", outcome.output)
        self.assertIn("call checkout.py:4: return calculate_total(100, 0.2)", outcome.output)
        self.assertNotIn("calculate_totalMock", outcome.output)

    def test_find_references_returns_typescript_reference_hints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "src"
            src.mkdir()
            Path(src, "router.ts").write_text(
                "export function createRouter() {\n"
                "  return router;\n"
                "}\n"
                "const createRouterMock = () => null;\n",
                encoding="utf-8",
            )
            Path(src, "server.ts").write_text(
                "import { createRouter } from './router';\n"
                "app.use(createRouter());\n",
                encoding="utf-8",
            )

            outcome = find_references(root, "src", "createRouter")

        self.assertTrue(outcome.ok)
        self.assertIn("export src/router.ts:1: export function createRouter()", outcome.output)
        self.assertIn("import src/server.ts:1: import { createRouter }", outcome.output)
        self.assertIn("call src/server.ts:2: app.use(createRouter())", outcome.output)
        self.assertNotIn("createRouterMock", outcome.output)

    def test_find_references_is_bounded_and_skips_excluded_and_unreadable_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(80):
                Path(root, f"use_{index:02}.py").write_text(
                    "target_symbol()\n",
                    encoding="utf-8",
                )
            Path(root, "z_over.py").write_text("target_symbol()\n", encoding="utf-8")
            node = root / "node_modules"
            node.mkdir()
            Path(node, "pkg.py").write_text("target_symbol()\n", encoding="utf-8")
            Path(root, "bad.py").write_bytes(b"\xff\xfe target_symbol")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("references truncated after 80 matches", outcome.output)
        self.assertNotIn("node_modules", outcome.output)
        self.assertNotIn("bad.py", outcome.output)
        self.assertNotIn("z_over.py", outcome.output)

    def test_find_references_skips_direct_excluded_start_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node = root / "node_modules"
            node.mkdir()
            Path(node, "pkg.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, "node_modules", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.output)
        self.assertNotIn("pkg.py", outcome.output)
        self.assertFalse(outcome.truncated)

    def test_find_references_skips_excluded_directories_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build = root / "Build"
            build.mkdir()
            Path(build, "out.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.output)
        self.assertNotIn("out.py", outcome.output)

    def test_find_references_skips_direct_excluded_start_directory_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node = root / "Node_Modules"
            node.mkdir()
            Path(node, "pkg.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, "Node_Modules", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.output)
        self.assertNotIn("pkg.py", outcome.output)
        self.assertFalse(outcome.truncated)

    def test_find_references_does_not_skip_project_root_named_like_excluded_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "build"
            root.mkdir()
            Path(root, "app.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("call app.py:1: target_symbol()", outcome.output)

    def test_find_references_marks_truncated_only_after_limit_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(80):
                Path(root, f"use_{index:02}.py").write_text(
                    "target_symbol()\n",
                    encoding="utf-8",
                )

            exact = find_references(root, ".", "target_symbol")
            Path(root, "z_over.py").write_text("target_symbol()\n", encoding="utf-8")
            over = find_references(root, ".", "target_symbol")

        self.assertTrue(exact.ok)
        self.assertFalse(exact.truncated)
        self.assertNotIn("references truncated", exact.output)
        self.assertTrue(over.ok)
        self.assertTrue(over.truncated)
        self.assertIn("references truncated after 80 matches", over.output)

    def test_find_references_reports_scan_budget_without_collecting_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "a.py").write_text("pass\n", encoding="utf-8")
            Path(root, "b.py").write_text("pass\n", encoding="utf-8")
            Path(root, "c.py").write_text("target_symbol()\n", encoding="utf-8")

            scan = find_reference_hints(
                root,
                root,
                "target_symbol",
                max_scan_files=2,
            )

        self.assertTrue(scan.truncated)
        self.assertIn("no lexical matches found", scan.output)
        self.assertIn("reference scan stopped after 2 files", scan.output)
        self.assertIn("file budget 2", scan.output)
        self.assertIn("omitted files may contain more matches", scan.output)
        self.assertNotIn("c.py:1", scan.output)

    def test_find_references_does_not_follow_symlinked_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td, "project")
            outside = Path(td, "outside")
            root.mkdir()
            outside.mkdir()
            Path(outside, "leak.py").write_text("target_symbol()\n", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.output)
        self.assertNotIn("leak.py", outcome.output)

    def test_find_references_rejects_direct_symlink_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "target.py"
            target.write_text("target_symbol()\n", encoding="utf-8")
            link = root / "link.py"
            try:
                link.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"file symlink unavailable: {exc}")

            outcome = find_references(root, "link.py", "target_symbol")

        self.assertFalse(outcome.ok)
        self.assertIn("symlink paths are not supported", outcome.output)
        self.assertNotIn("target.py:1", outcome.output)

    def test_find_references_rejects_direct_symlink_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target_dir = root / "src"
            target_dir.mkdir()
            (target_dir / "target.py").write_text("target_symbol()\n", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(target_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            outcome = find_references(root, "linked", "target_symbol")

        self.assertFalse(outcome.ok)
        self.assertIn("symlink paths are not supported", outcome.output)
        self.assertNotIn("target.py:1", outcome.output)

    def test_find_references_validates_simple_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outcome = find_references(Path(td), ".", "foo.bar")

        self.assertFalse(outcome.ok)
        self.assertIn("requires a simple symbol", outcome.output)

    def test_write_and_edit_reject_oversized_result_without_changing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.txt"
            path.write_text("old", encoding="utf-8")
            blocks = [EditBlock("old", "large")]

            with mock.patch("codey.tool_runtime.WRITE_MAX_FILE_BYTES", 4):
                written = write_file(root, "new.txt", "large")
                edited = edit_file(root, "app.txt", blocks)

            self.assertFalse(written.ok)
            self.assertFalse((root / "new.txt").exists())
            self.assertFalse(edited.ok)
            self.assertEqual(path.read_text(encoding="utf-8"), "old")

    def test_multiple_replacements_are_written_once_after_all_validate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.txt"
            path.write_text("one\ntwo\nthree\n", encoding="utf-8")
            blocks = [
                EditBlock("one", "ONE"),
                EditBlock("two", "TWO"),
                EditBlock("missing", "THREE"),
            ]

            failed = edit_file(root, "app.txt", blocks)
            remaining = path.read_text(encoding="utf-8")

        self.assertFalse(failed.ok)
        self.assertEqual(remaining, "one\ntwo\nthree\n")

    def test_runtime_rejects_more_than_maximum_replacements(self) -> None:
        blocks = [
            EditBlock(str(index), "x")
            for index in range(MAX_REPLACEMENTS + 1)
        ]
        original = "\n".join(str(index) for index in range(MAX_REPLACEMENTS + 1))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.txt"
            path.write_text(original, encoding="utf-8")

            outcome = edit_file(root, "app.txt", blocks)
            remaining = path.read_text(encoding="utf-8")

        self.assertFalse(outcome.ok)
        self.assertIn(f"at most {MAX_REPLACEMENTS}", outcome.output)
        self.assertEqual(remaining, original)


if __name__ == "__main__":
    unittest.main()
