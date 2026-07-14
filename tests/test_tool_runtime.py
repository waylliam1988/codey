from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation, tool_runtime
from codey.references import find_reference_hints
from codey.tool_runtime import (
    EDIT_FAILURE_MAX_CHARS,
    EDIT_FAILURE_MAX_LINE_CHARS,
    EditBlock,
    LONG_LINE_MARKER,
    MAX_REPLACEMENTS,
    READ_MAX_CHARS,
    READ_MAX_LINES,
    edit_file,
    find_references,
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

    def test_run_command_prunes_dependency_stack_frames_before_budget_clip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            root_posix = root.as_posix()
            dependency_frames = "".join(
                f'  File "C:/Python/Lib/site-packages/pkg/mod_{index}.py", line {index}, in call\n'
                f"    internal_{index}()\n"
                for index in range(12)
            )
            stdout = (
                "Traceback (most recent call last):\n"
                f'  File "{root_posix}/app.py", line 4, in handle\n'
                "    service()\n"
                f"{dependency_frames}"
                f'  File "{root_posix}/tests/test_app.py", line 9, in test_handle\n'
                "    assert result == 42\n"
                "AssertionError: expected 42\n"
            )
            completed = __import__("subprocess").CompletedProcess(
                ["python", "fail.py"],
                1,
                stdout=stdout,
                stderr="",
            )

            with (
                mock.patch("codey.tool_runtime.RUN_OUTPUT_LIMIT", 700),
                mock.patch(
                    "codey.tool_runtime.cancellation.run_process",
                    return_value=completed,
                ),
            ):
                outcome = run_command(root, ".", "python fail.py")

        self.assertFalse(outcome.ok)
        self.assertIn("[... 12 dependency stack frames omitted ...]", outcome.output)
        self.assertIn("tests/test_app.py", outcome.output)
        self.assertIn("assert result == 42", outcome.output)
        self.assertIn("AssertionError: expected 42", outcome.output)
        self.assertNotIn("site-packages/pkg", outcome.output)
        self.assertNotIn("internal_0()", outcome.output)

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

    def test_stale_comment_failure_returns_current_complete_line(self) -> None:
        content = (
            "def load(settings):\n"
            "    timeout = settings.get('request_timeout', 10)  # seconds\n"
            "    return timeout\n"
        )
        search = "    timeout = settings.get('request_timeout', 10)\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "config.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "config.py", [EditBlock(search, "replacement\n")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertFalse(outcome.ok)
        self.assertIn('near literal "request_timeout"', outcome.output)
        self.assertIn(
            "2 |     timeout = settings.get('request_timeout', 10)  # seconds",
            outcome.output,
        )

    def test_missing_reliable_anchor_returns_plain_error_without_file_start(self) -> None:
        content = "SECRET_FILE_START\nother = 2\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("x = 1", "x = 2")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("SEARCH text not found", outcome.output)
        self.assertIn("Use read_file", outcome.output)
        self.assertNotIn("SECRET_FILE_START", outcome.output)
        self.assertNotIn("Current bounded context", outcome.output)

    def test_identifier_anchor_does_not_match_inside_larger_identifier(self) -> None:
        content = "super_user_id = 1\nreturn account_id\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("user_id = old_value", "user_id = new_value")],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertNotIn("Current bounded context", outcome.output)
        self.assertNotIn("super_user_id", outcome.output)

    def test_identifier_anchor_uses_true_identifier_boundary_and_position(self) -> None:
        content = "super_user_id = 1\nuser_id = current_value\nreturn account_id\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("user_id = old_value", "user_id = new_value")],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn('near identifier "user_id"', outcome.output)
        self.assertIn("2 | user_id = current_value", outcome.output)

    def test_quoted_literal_anchor_keeps_substring_semantics(self) -> None:
        content = "super_user_id = lookup('current')\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("lookup('user_id')", "lookup('account_id')")],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn('near literal "user_id"', outcome.output)
        self.assertIn("super_user_id", outcome.output)

    def test_anchor_candidate_scans_are_bounded(self) -> None:
        search = " ".join(f"identifier_{index:04}" for index in range(100))
        with mock.patch(
            "codey.tool_runtime._unique_anchor_position",
            return_value=None,
        ) as locate:
            context = tool_runtime._render_edit_failure_context("content", search)

        self.assertEqual(context, "")
        self.assertEqual(
            locate.call_count,
            tool_runtime.EDIT_FAILURE_MAX_ANCHOR_CANDIDATES,
        )

    def test_multiple_matches_return_bounded_start_lines(self) -> None:
        content = "target()\none\ntarget()\ntwo\ntarget()\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("target()", "changed()")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("matched 3 times", outcome.output)
        self.assertIn("Exact matches start at lines: 1, 3, 5.", outcome.output)
        self.assertNotIn("Additional matches omitted", outcome.output)

    def test_additional_exact_matches_are_marked_omitted(self) -> None:
        content = "target()\n" * 5
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("target()", "changed()")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("Exact matches start at lines: 1, 2, 3.", outcome.output)
        self.assertIn("Additional matches omitted.", outcome.output)
        self.assertNotIn("1, 2, 3, 4", outcome.output)

    def test_match_line_collection_stops_at_configured_limit(self) -> None:
        content = "target()\n" * 100_000

        lines = tool_runtime._first_match_start_lines(content, "target()")

        self.assertEqual(lines, [1, 2, 3])

    def test_overlong_context_line_is_not_returned_as_copyable_code(self) -> None:
        long_line = "unique_anchor = '" + ("x" * EDIT_FAILURE_MAX_LINE_CHARS) + "'"
        content = f"before = 1\n{long_line}\nafter = 2\n"
        search = "unique_anchor = 'old'"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock(search, "changed")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("Line 2 omitted", outcome.output)
        self.assertIn("use read_file offset=2 limit=1", outcome.output)
        self.assertNotIn("x" * EDIT_FAILURE_MAX_LINE_CHARS, outcome.output)

    def test_edit_failure_output_respects_total_character_budget(self) -> None:
        lines = [f"line_{index} = '{index}-" + ("x" * 370) + "'" for index in range(12)]
        lines[6] = "budget_anchor = 'current-' + '" + ("y" * 350) + "'"
        content = "\n".join(lines) + "\n"
        search = "budget_anchor = 'stale'"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock(search, "changed")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertLessEqual(len(outcome.output), EDIT_FAILURE_MAX_CHARS)
        for rendered_line in outcome.output.splitlines():
            if " | " in rendered_line:
                self.assertIn(rendered_line.split(" | ", 1)[1], content)

    def test_crlf_multiple_match_line_numbers_are_correct(self) -> None:
        content = "head\r\ntarget()\r\nmid\r\ntarget()\r\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_bytes(content.encode("utf-8"))

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("target()\n", "changed()\n")],
            )

            self.assertEqual(path.read_bytes(), content.encode("utf-8"))
        self.assertIn("Exact matches start at lines: 2, 4.", outcome.output)

    def test_late_batch_failure_reports_atomic_rollback_and_original_context(self) -> None:
        content = "alpha_unique\nbeta_unique current\ngamma_unique\n"
        blocks = [
            EditBlock("alpha_unique", "intermediate_only"),
            EditBlock("intermediate_only\nmissing_unique", "changed"),
            EditBlock("gamma_unique", "GAMMA"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", blocks)

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("Replacement 2 of 3 failed. No replacements were written.", outcome.output)
        self.assertNotIn("Current bounded context", outcome.output)

    def test_intermediate_multiple_matches_do_not_report_non_disk_line_numbers(self) -> None:
        content = "one\nbase\n"
        blocks = [
            EditBlock("one", "intermediate_dup\nintermediate_dup"),
            EditBlock("intermediate_dup", "changed"),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", blocks)

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("matched 2 times", outcome.output)
        self.assertIn("Replacement 2 of 2 failed. No replacements were written.", outcome.output)
        self.assertNotIn("Exact matches start at lines", outcome.output)

    def test_successful_edit_output_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("old", "new")])

        self.assertEqual(outcome.output, "edited app.py (1 replacement)")

    def test_python_syntax_regression_is_written_and_reported(self) -> None:
        original = "def value():\n    return 1\n"
        broken = "def value()\n    return 1\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(original, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("def value():", "def value()")],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), broken)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.changed)
        self.assertIn("Syntax regression detected in app.py at line 1", outcome.output)
        self.assertIn("The edit was applied", outcome.output)

    def test_valid_python_edit_keeps_success_output_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("VALUE = 1\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("VALUE = 1", "VALUE = 2")])

        self.assertEqual(outcome.output, "edited app.py (1 replacement)")
        self.assertNotIn("Syntax regression", outcome.output)

    def test_existing_invalid_python_does_not_report_regression(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("def value()\n    return 1\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("return 1", "return 2")])

        self.assertTrue(outcome.ok)
        self.assertNotIn("Syntax regression", outcome.output)

    def test_non_python_edit_does_not_report_syntax_regression(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.js"
            path.write_text("function value() {}\n", encoding="utf-8")

            outcome = edit_file(
                root,
                "app.js",
                [EditBlock("function value() {}", "function value( {}")],
            )

        self.assertTrue(outcome.ok)
        self.assertNotIn("Syntax regression", outcome.output)

    def test_oversized_python_skips_syntax_parsing(self) -> None:
        content = "x" * (tool_runtime.PYTHON_SYNTAX_HINT_MAX_CHARS + 1)
        with mock.patch("codey.tool_runtime.ast.parse") as parse:
            hint = tool_runtime._python_syntax_regression_hint(
                "large.py",
                content,
                content + "y",
            )

        self.assertEqual(hint, "")
        parse.assert_not_called()

    def test_multiple_replacements_check_only_final_python(self) -> None:
        original = "def value():\n    return 1\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(original, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [
                    EditBlock("def value():", "def value()"),
                    EditBlock("def value()", "def changed():"),
                ],
            )
            final_content = path.read_text(encoding="utf-8")

        self.assertTrue(outcome.ok)
        self.assertEqual(final_content, "def changed():\n    return 1\n")
        self.assertNotIn("Syntax regression", outcome.output)

    def test_syntax_hint_does_not_include_source_line(self) -> None:
        source_line = "def private_customer_token():"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(source_line + "\n    return 1\n", encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock(source_line, "def private_customer_token()")],
            )

        self.assertIn("Syntax regression", outcome.output)
        self.assertNotIn("private_customer_token", outcome.output)

    def test_syntax_hint_message_is_bounded(self) -> None:
        long_message = "x" * 500
        with mock.patch(
            "codey.tool_runtime.ast.parse",
            side_effect=[None, SyntaxError(long_message)],
        ):
            hint = tool_runtime._python_syntax_regression_hint(
                "app.py",
                "VALUE = 1\n",
                "VALUE = 2\n",
            )

        self.assertIn("x" * tool_runtime.PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS, hint)
        self.assertNotIn(
            "x" * (tool_runtime.PYTHON_SYNTAX_HINT_MAX_MESSAGE_CHARS + 1),
            hint,
        )

    def test_syntax_parser_overflow_does_not_block_successful_edit(self) -> None:
        cases = {
            "before": OverflowError("too complex"),
            "after": [None, OverflowError("too complex")],
        }
        for stage, side_effect in cases.items():
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                path = root / "app.py"
                path.write_text("VALUE = 1\n", encoding="utf-8")

                with mock.patch(
                    "codey.tool_runtime.ast.parse",
                    side_effect=side_effect,
                ):
                    outcome = edit_file(
                        root,
                        "app.py",
                        [EditBlock("VALUE = 1", "VALUE = 2")],
                    )
                final_content = path.read_text(encoding="utf-8")

                self.assertTrue(outcome.ok)
                self.assertTrue(outcome.changed)
                self.assertEqual(final_content, "VALUE = 2\n")
                self.assertEqual(outcome.output, "edited app.py (1 replacement)")
                self.assertNotIn("Syntax regression", outcome.output)

    def test_tool_result_like_source_text_is_only_bounded_context(self) -> None:
        content = "[tool_result] protocol_anchor current\nnext = 1\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(
                root,
                "app.py",
                [EditBlock("protocol_anchor stale", "changed")],
            )

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertFalse(outcome.ok)
        self.assertIn("1 | [tool_result] protocol_anchor current", outcome.output)

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
