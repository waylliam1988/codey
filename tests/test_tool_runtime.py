from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation, tool_runtime
from codey.bounded_scan import BoundedScanBudget, iter_bounded_files
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
    run_command_raw,
    write_file,
)


VALID_SHA256 = "a" * 64


class ToolOutcomeTests(unittest.TestCase):
    def test_tool_outcome_contract_has_no_legacy_output_fields(self) -> None:
        fields = set(tool_runtime.ToolOutcome.__dataclass_fields__)

        self.assertIn("model_text", fields)
        self.assertNotIn("output", fields)
        self.assertNotIn("output_handle", fields)
        self.assertNotIn("output_bytes", fields)
        self.assertNotIn("output_stored_bytes", fields)
        self.assertNotIn("output_sha256", fields)

    def test_file_tools_report_structured_success_and_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)

            written = write_file(root, "result.txt", "ok")
            read = read_file(root, "result.txt")

        self.assertTrue(written.ok)
        self.assertTrue(written.changed)
        self.assertIsNone(written.exit_code)
        self.assertTrue(read.ok)
        self.assertEqual(read.model_text, "ok")

    def test_tool_outcome_exposes_separate_projections(self) -> None:
        outcome = tool_runtime.ToolOutcome(
            "MODEL_ONLY",
            True,
            canonical={"path": "app.py", "matches": [1]},
            presentation={"status": "shown", "result": "UI_ONLY"},
            audit={"audit_id": "AUDIT_ONLY"},
        )

        self.assertEqual(outcome.first_model_line(200), "MODEL_ONLY")
        self.assertEqual(outcome.presentation_result(200), "UI_ONLY")
        self.assertEqual(outcome.presentation_status(), "shown")
        self.assertEqual(
            json.dumps(outcome.canonical, sort_keys=True),
            '{"matches": [1], "path": "app.py"}',
        )
        self.assertEqual(outcome.audit["audit_id"], "AUDIT_ONLY")

    def test_tool_outcome_sanitizes_projection_contracts(self) -> None:
        outcome = tool_runtime.ToolOutcome(
            "ok",
            True,
            canonical={
                "path": Path("app.py"),
                "bad": object(),
                "nan": float("nan"),
                "long": "x" * 3_000,
            },
            presentation={
                "status": "shown",
                "result": "UI",
                1: object(),
            },
            audit={"bad": object(), "items": [object()]},
        )

        json.dumps(outcome.presentation, allow_nan=False)
        json.dumps(outcome.audit, allow_nan=False)
        json.dumps(outcome.canonical, allow_nan=False)
        self.assertEqual(outcome.presentation_status(), "shown")
        self.assertEqual(outcome.presentation_result(20), "UI")
        self.assertTrue(str(outcome.canonical["path"]).startswith("<non-json "))
        self.assertTrue(str(outcome.canonical["path"]).endswith("Path>"))
        self.assertEqual(outcome.canonical["bad"], "<non-json object>")
        self.assertEqual(outcome.canonical["nan"], "nan")
        self.assertEqual(len(str(outcome.canonical["long"])), 2_000)
        self.assertIn("_projection_warnings", outcome.presentation)
        self.assertIn("_projection_warnings", outcome.audit)
        self.assertIn("_projection_warnings", outcome.canonical)

    def test_tool_outcome_sanitizes_non_mapping_projections(self) -> None:
        outcome = tool_runtime.ToolOutcome(
            "ok",
            True,
            canonical=object(),
            presentation=object(),
            audit=object(),
        )

        json.dumps(outcome.presentation, allow_nan=False)
        json.dumps(outcome.audit, allow_nan=False)
        json.dumps(outcome.canonical, allow_nan=False)
        self.assertEqual(outcome.presentation_status(), "ok")
        self.assertEqual(outcome.presentation_result(20), "ok")
        self.assertIn("_projection_warnings", outcome.presentation)
        self.assertIn("_projection_warnings", outcome.audit)
        self.assertIn("_projection_warnings", outcome.canonical)

    def test_tool_outcome_ignores_invalid_managed_output_handles(self) -> None:
        for handle in ("../x", "out_", "out_" + ("x" * 81), 123, float("nan")):
            with self.subTest(handle=handle):
                outcome = tool_runtime.ToolOutcome(
                    "ok",
                    True,
                    audit={
                        "managed_output": {
                            "handle": handle,
                            "original_bytes": 1234,
                            "stored_bytes": 1000,
                            "sha256": VALID_SHA256,
                        }
                    },
                )

                self.assertEqual(outcome.managed_output(), {})
                self.assertNotIn("full output retained locally", outcome.model_text)

    def test_tool_outcome_empties_invalid_managed_output_sha256(self) -> None:
        for sha256 in ("abc\nINJECTED", object(), "z" * 64):
            with self.subTest(sha256=sha256):
                outcome = tool_runtime.ToolOutcome(
                    "ok",
                    True,
                    audit={
                        "managed_output": {
                            "handle": "out_0001_valid",
                            "original_bytes": 1234,
                            "stored_bytes": 1000,
                            "sha256": sha256,
                        }
                    },
                )

                self.assertEqual(outcome.managed_output()["sha256"], "")
                self.assertIn("sha256=;", outcome.model_text)
                self.assertNotIn("INJECTED", outcome.model_text)
                self.assertNotIn("<non-json object>", outcome.model_text)

    def test_tool_outcome_bounds_projection_depth_keys_and_items(self) -> None:
        deep: dict[str, object] = {"leaf": "value"}
        for _index in range(10):
            deep = {"next": deep}

        outcome = tool_runtime.ToolOutcome(
            "ok",
            True,
            canonical={
                "x" * 120: "value",
                "deep": deep,
                "items": list(range(300)),
            },
        )

        json.dumps(outcome.canonical, allow_nan=False)
        clipped_key = next(key for key in outcome.canonical if key.startswith("x"))
        self.assertEqual(len(clipped_key), 80)
        self.assertLess(len(outcome.canonical["items"]), 300)
        value = outcome.canonical["deep"]
        while isinstance(value, dict):
            value = next(iter(value.values()))
        self.assertTrue(str(value).startswith("<max-depth "))
        self.assertIn("_projection_warnings", outcome.canonical)

    def test_failed_check_has_exit_code_without_becoming_runtime_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "fail.py").write_text("raise SystemExit(3)\n", encoding="utf-8")

            outcome = run_command(root, ".", "python fail.py")

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.exit_code, 3)
        self.assertTrue(outcome.model_text.startswith("exit 3:"))
        self.assertFalse(outcome.model_text.startswith("ERROR:"))

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
        self.assertIn("HEAD", outcome.model_text)
        self.assertIn("TAIL", outcome.model_text)
        self.assertIn("middle of output omitted", outcome.model_text)
        self.assertIn("omitted content may contain relevant errors", outcome.model_text)

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
        self.assertIn("[... 12 dependency stack frames omitted ...]", outcome.model_text)
        self.assertIn("tests/test_app.py", outcome.model_text)
        self.assertIn("assert result == 42", outcome.model_text)
        self.assertIn("AssertionError: expected 42", outcome.model_text)
        self.assertNotIn("site-packages/pkg", outcome.model_text)
        self.assertNotIn("internal_0()", outcome.model_text)

    def test_run_command_raw_preserves_output_before_projection_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stdout = (
                "Traceback (most recent call last):\n"
                '  File "C:/Python/Lib/site-packages/pkg/mod.py", line 1, in call\n'
                "    internal_call()\n"
                "AssertionError: expected 42\n"
            )
            completed = subprocess.CompletedProcess(
                ["python", "fail.py"],
                1,
                stdout=stdout,
                stderr="",
            )

            with mock.patch(
                "codey.tool_runtime.cancellation.run_process",
                return_value=completed,
            ):
                raw = run_command_raw(root, ".", "python fail.py")
                outcome = tool_runtime.project_run_command_result(root, raw)

        self.assertIsInstance(raw, tool_runtime.RunCommandRawResult)
        assert isinstance(raw, tool_runtime.RunCommandRawResult)
        self.assertIn("site-packages/pkg", raw.output)
        self.assertNotIn("site-packages/pkg", outcome.model_text)
        self.assertIn("AssertionError: expected 42", outcome.model_text)

    def test_run_allowlist_accepts_common_verification_tools(self) -> None:
        for command in (
            "ruff check .",
            "ruff format --check .",
            "mypy codey",
            "python -m mypy codey",
            "python -m ruff check .",
            "python -m ruff format --check .",
            "make test",
            "make lint check",
            "bun test",
            "bun run build",
            "deno test",
            "deno lint",
            "deno fmt --check",
        ):
            argv = command.split()
            self.assertTrue(
                tool_runtime._is_allowed_run_command(argv),
                f"expected allowed: {command}",
            )

    def test_run_allowlist_blocks_file_mutating_and_installing_forms(self) -> None:
        for command in (
            "ruff format .",
            "ruff check --fix .",
            "ruff check --fix-only .",
            "ruff check --unsafe-fixes .",
            "ruff check --add-noqa .",
            "ruff check --output-file report.txt .",
            "ruff check --output-file=report.txt .",
            "python -m ruff format .",
            "python -m ruff check --fix .",
            "python -m ruff check --add-noqa .",
            "python -m ruff check --output-file report.txt .",
            "python -m ruff clean",
            "deno fmt",
            "mypy --install-types --non-interactive codey",
            "python -m mypy --install-types codey",
            "bun build ./src/index.ts --outdir dist",
            "bun install",
            "bun lint",
            "bun check",
            "bun typecheck",
            "python -m compileall .",
            "python -m compileall",
            "ruff check --unsafe-fixes=true .",
            "mypy --install-types=1 codey",
            "python -m mypy --install-types=1 codey",
        ):
            argv = command.split()
            self.assertFalse(
                tool_runtime._is_allowed_run_command(argv),
                f"expected rejected: {command}",
            )

    def test_suite_classification_never_exceeds_allowlist(self) -> None:
        for command in (
            "bun build ./src/index.ts --outdir dist",
            "mypy --install-types codey",
            "python -m compileall .",
        ):
            self.assertFalse(
                tool_runtime._is_suite_run_command(command.split()),
                f"disallowed command must not be a suite: {command}",
            )
        for command in (
            "pytest",
            "bun test",
            "bun run build",
            "python -m unittest",
            "make test",
        ):
            self.assertTrue(
                tool_runtime._is_suite_run_command(command.split()),
                f"expected suite: {command}",
            )

    def test_run_allowlist_still_rejects_unlisted_and_unsafe_commands(self) -> None:
        for command in (
            "pip install requests",
            "ruff clean",
            "make deploy",
            "deno run server.ts",
            "bun add left-pad",
            "rm -rf .",
            "python -m http.server",
        ):
            argv = command.split()
            self.assertFalse(
                tool_runtime._is_allowed_run_command(argv),
                f"expected rejected: {command}",
            )

    def test_suite_commands_report_timeout_distinct_from_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch(
                "codey.tool_runtime.cancellation.run_process",
                side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=300),
            ),
        ):
            outcome = run_command(Path(td), ".", "pytest")

        self.assertFalse(outcome.ok)
        self.assertIn(f"{tool_runtime.RUN_SUITE_TIMEOUT_SECONDS}s", outcome.model_text)
        self.assertIn("timeout, not a test failure", outcome.model_text)

    def test_quick_commands_keep_the_default_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
            with mock.patch(
                "codey.tool_runtime.cancellation.run_process",
                side_effect=subprocess.TimeoutExpired(cmd="python app.py", timeout=90),
            ):
                outcome = run_command(root, ".", "python app.py")

        self.assertFalse(outcome.ok)
        self.assertIn(f"{tool_runtime.RUN_TIMEOUT_SECONDS}s", outcome.model_text)
        self.assertNotIn(f"{tool_runtime.RUN_SUITE_TIMEOUT_SECONDS}s", outcome.model_text)

    def test_search_truncation_hint_suggests_narrowing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.py").write_text("target\ntarget\ntarget\n", encoding="utf-8")

            outcome = tool_runtime.search_files(root, ".", "target", max_results=2)

        self.assertTrue(outcome.truncated)
        self.assertIn("truncated after 2 matches", outcome.model_text)
        self.assertIn("narrow the query", outcome.model_text)

    def test_search_reports_non_utf8_files_as_incomplete(self) -> None:
        marker = "RARE_NON_UTF8_SEARCH_MARKER"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text(
                "def healthcheck():\n    return True\n",
                encoding="utf-8",
            )
            (root / "binary.dat").write_bytes(b"\xff\xfe\x00" + marker.encode("ascii"))

            outcome = tool_runtime.search_files(root, ".", marker)

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.model_text)
        self.assertIn("Scan coverage:", outcome.model_text)
        self.assertIn("search skipped 1 non-UTF-8 file", outcome.model_text)
        self.assertIn("omitted files may contain more matches", outcome.model_text)
        self.assertNotIn(marker, outcome.model_text)

    def test_search_reports_read_text_errors_as_incomplete(self) -> None:
        original_read_text = Path.read_text

        def read_text(path: Path, *args, **kwargs) -> str:
            if path.name == "unreadable.py":
                raise OSError("synthetic unreadable file")
            return original_read_text(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text(
                "def healthcheck():\n    return True\n",
                encoding="utf-8",
            )
            (root / "unreadable.py").write_text("target_marker\n", encoding="utf-8")

            with mock.patch.object(Path, "read_text", read_text):
                outcome = tool_runtime.search_files(root, ".", "target_marker")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.model_text)
        self.assertIn("Scan coverage:", outcome.model_text)
        self.assertIn(
            "search could not read metadata or contents for 1 file",
            outcome.model_text,
        )
        self.assertNotIn("target_marker", outcome.model_text)

    def test_search_reports_stat_errors_as_incomplete(self) -> None:
        original_stat = Path.stat

        def stat(path: Path, *args, **kwargs):
            if path.name == "stat_unreadable.py":
                import traceback

                caller = traceback.extract_stack(limit=2)[0].name
                if caller == "search_files":
                    raise OSError("synthetic stat failure")
            return original_stat(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "app.py").write_text(
                "def healthcheck():\n    return True\n",
                encoding="utf-8",
            )
            (root / "stat_unreadable.py").write_text("target_marker\n", encoding="utf-8")

            with mock.patch.object(Path, "stat", stat):
                outcome = tool_runtime.search_files(root, ".", "target_marker")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("no literal matches", outcome.model_text)
        self.assertIn("Scan coverage:", outcome.model_text)
        self.assertIn(
            "search could not read metadata or contents for 1 file",
            outcome.model_text,
        )
        self.assertNotIn("target_marker", outcome.model_text)

    def test_search_does_not_duplicate_coverage_for_existing_oversized_and_budget_notes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "huge.py").write_text("x" * 33, encoding="utf-8")
            with mock.patch("codey.tool_runtime.SEARCH_MAX_FILE_BYTES", 32):
                oversized = tool_runtime.search_files(root, "huge.py", "target")

            (root / "a.py").write_text("pass\n", encoding="utf-8")
            (root / "b.py").write_text("pass\n", encoding="utf-8")
            (root / "c.py").write_text("target\n", encoding="utf-8")
            with mock.patch("codey.tool_runtime.SEARCH_MAX_SCAN_FILES", 2):
                budget = tool_runtime.search_files(root, ".", "target")

        self.assertIn("skipped 1 file(s) larger than 32 bytes", oversized.model_text)
        self.assertNotIn("Scan coverage:", oversized.model_text)
        self.assertIn("search scan stopped after 2 files", budget.model_text)
        self.assertNotIn("Scan coverage:", budget.model_text)

    def test_writing_identical_content_is_not_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "same.txt"
            path.write_text("same", encoding="utf-8")

            outcome = write_file(root, "same.txt", "same")

        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertIn("no changes", outcome.model_text)

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
        self.assertTrue(first.model_text.startswith("one\ntwo\n"))
        self.assertIn("lines 1-2 of 5; next offset=3", first.model_text)
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"app.txt","offset":3,"limit":2}}',
            first.model_text,
        )
        self.assertTrue(middle.model_text.startswith("three\nfour\n"))
        self.assertIn("next offset=5", middle.model_text)
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"app.txt","offset":5,"limit":2}}',
            middle.model_text,
        )
        self.assertTrue(last.model_text.startswith("five\n"))
        self.assertIn("lines 5-5 of 5", last.model_text)
        self.assertNotIn("next offset=", last.model_text)
        self.assertNotIn("next call:", last.model_text)

    def test_read_file_character_budget_never_splits_a_normal_line(self) -> None:
        first_line = "a" * (READ_MAX_CHARS // 2) + "\n"
        second_line = "b" * (READ_MAX_CHARS // 2) + "\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "large.txt").write_text(first_line + second_line, encoding="utf-8")

            outcome = read_file(root, "large.txt")

        content, metadata = outcome.model_text.rsplit("\n\n[", 1)
        self.assertEqual(content, first_line.rstrip("\n"))
        self.assertNotIn("b", content)
        self.assertIn("lines 1-1 of 2; next offset=2", metadata)
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"large.txt","offset":2,"limit":300}}',
            metadata,
        )

    def test_read_file_marks_overlong_line_as_preview_only(self) -> None:
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
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"generated.txt","offset":2,"limit":300}}',
            outcome.model_text,
        )

    def test_read_file_next_call_escapes_path_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / "nested"
            nested.mkdir()
            path = nested / "page.txt"
            path.write_text("one\ntwo\n", encoding="utf-8")

            outcome = read_file(root, "nested\\page.txt", limit=1)

        self.assertTrue(outcome.truncated)
        self.assertIn(
            'next call: {"tool":"read_file","args":{"path":"nested\\\\page.txt","offset":2,"limit":1}}',
            outcome.model_text,
        )

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

        self.assertEqual(empty.model_text, "")
        self.assertFalse(past_end.ok)
        self.assertIn("exceeds one.txt total lines 1", past_end.model_text)
        self.assertIn("positive integer", zero.model_text)
        self.assertIn("positive integer", fractional.model_text)
        self.assertIn(f"at most {READ_MAX_LINES}", oversized.model_text)

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
        self.assertIn("References for calculate_total under .", outcome.model_text)
        self.assertIn("lexical scan, not semantic resolution", outcome.model_text)
        self.assertIn("definition pricing.py:1: def calculate_total", outcome.model_text)
        self.assertIn("import checkout.py:1: from pricing import calculate_total", outcome.model_text)
        self.assertIn("call checkout.py:4: return calculate_total(100, 0.2)", outcome.model_text)
        self.assertNotIn("calculate_totalMock", outcome.model_text)

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
        self.assertIn("export src/router.ts:1: export function createRouter()", outcome.model_text)
        self.assertIn("import src/server.ts:1: import { createRouter }", outcome.model_text)
        self.assertIn("call src/server.ts:2: app.use(createRouter())", outcome.model_text)
        self.assertNotIn("createRouterMock", outcome.model_text)

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
        self.assertIn("references truncated after 80 matches", outcome.model_text)
        self.assertNotIn("node_modules", outcome.model_text)
        self.assertIn("Scan coverage:", outcome.model_text)
        self.assertIn("skipped 1 non-UTF-8 file", outcome.model_text)
        self.assertIn("bad.py", outcome.model_text)
        self.assertNotIn("z_over.py", outcome.model_text)

    def test_writer_find_references_reports_oversized_files_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "payments.py").write_text(
                "def process_payment(order):\n    return order\n",
                encoding="utf-8",
            )
            Path(root, "legacy.py").write_text(
                "# padding\n" * 60_000
                + "from payments import process_payment\n"
                + "process_payment({})\n",
                encoding="utf-8",
            )

            outcome = find_references(root, ".", "process_payment")

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.truncated)
        self.assertIn("definition payments.py:1", outcome.model_text)
        self.assertIn("Scan coverage:", outcome.model_text)
        self.assertIn("skipped 1 oversized file over 512 KiB", outcome.model_text)
        self.assertIn("oversized path examples: legacy.py", outcome.model_text)
        self.assertNotIn("from payments import process_payment", outcome.model_text)

    def test_low_level_reference_hints_report_omissions_without_rendering_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            Path(root, "payments.py").write_text(
                "def process_payment(order):\n    return order\n",
                encoding="utf-8",
            )
            Path(root, "legacy.py").write_text(
                "# padding\n" * 60_000 + "process_payment({})\n",
                encoding="utf-8",
            )

            scan = find_reference_hints(root, root, "process_payment")

        self.assertFalse(scan.truncated)
        self.assertIsNotNone(scan.report)
        self.assertTrue(scan.report.incomplete)
        self.assertEqual(scan.report.oversized_examples, ["legacy.py"])
        self.assertNotIn("Scan coverage:", scan.output)

    def test_reference_report_records_unreadable_files_without_chmod(self) -> None:
        class UnreadablePath:
            def __init__(self, root: Path) -> None:
                self._root = root

            def relative_to(self, root: Path) -> Path:
                if root != self._root:
                    raise ValueError
                return Path("unreadable.py")

            def is_file(self) -> bool:
                return True

            def stat(self):
                raise OSError("stat failed")

            def read_text(self, *, encoding: str) -> str:
                raise AssertionError("read_text should not be called after stat failure")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            scan = find_reference_hints(
                root,
                root,
                "target_symbol",
                files=(UnreadablePath(root),),
                files_budgeted=True,
            )

        self.assertFalse(scan.truncated)
        self.assertIsNotNone(scan.report)
        self.assertTrue(scan.report.incomplete)
        self.assertEqual(scan.report.unreadable, 1)
        self.assertEqual(scan.report.unreadable_examples, ["unreadable.py"])
        self.assertNotIn("Scan coverage:", scan.output)

    def test_find_references_skips_direct_excluded_start_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node = root / "node_modules"
            node.mkdir()
            Path(node, "pkg.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, "node_modules", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.model_text)
        self.assertNotIn("pkg.py", outcome.model_text)
        self.assertFalse(outcome.truncated)

    def test_find_references_skips_excluded_directories_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            build = root / "Build"
            build.mkdir()
            Path(build, "out.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.model_text)
        self.assertNotIn("out.py", outcome.model_text)

    def test_find_references_skips_direct_excluded_start_directory_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            node = root / "Node_Modules"
            node.mkdir()
            Path(node, "pkg.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, "Node_Modules", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("no lexical matches found", outcome.model_text)
        self.assertNotIn("pkg.py", outcome.model_text)
        self.assertFalse(outcome.truncated)

    def test_find_references_does_not_skip_project_root_named_like_excluded_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "build"
            root.mkdir()
            Path(root, "app.py").write_text("target_symbol()\n", encoding="utf-8")

            outcome = find_references(root, ".", "target_symbol")

        self.assertTrue(outcome.ok)
        self.assertIn("call app.py:1: target_symbol()", outcome.model_text)

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
        self.assertNotIn("references truncated", exact.model_text)
        self.assertTrue(over.ok)
        self.assertTrue(over.truncated)
        self.assertIn("references truncated after 80 matches", over.model_text)

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

    def test_find_references_applies_byte_budget_to_provided_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "a.py"
            second = root / "b.py"
            first.write_text("target_symbol()\n", encoding="utf-8")
            second.write_text("target_symbol()\n", encoding="utf-8")
            budget = BoundedScanBudget(max_files=10, max_bytes=first.stat().st_size + 1)

            scan = find_reference_hints(
                root,
                root,
                "target_symbol",
                files=(first, second),
                scan_budget=budget,
            )

        self.assertTrue(scan.truncated)
        self.assertIn("a.py:1", scan.output)
        self.assertNotIn("b.py:1", scan.output)
        self.assertIn("byte budget", scan.output)

    def test_find_references_does_not_double_count_budgeted_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(3):
                Path(root, f"f{index}.py").write_text(
                    "target_symbol()\n",
                    encoding="utf-8",
                )
            budget = BoundedScanBudget(max_files=2, max_dirs=5)
            files = iter_bounded_files(root, excluded_dirs=set(), budget=budget)

            scan = find_reference_hints(
                root,
                root,
                "target_symbol",
                files=files,
                scan_budget=budget,
                files_budgeted=True,
            )

        self.assertTrue(scan.truncated)
        self.assertIn("f0.py:1", scan.output)
        self.assertIn("f1.py:1", scan.output)
        self.assertNotIn("f2.py:1", scan.output)
        self.assertIn("reference scan stopped after 2 files", scan.output)

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
        self.assertIn("no lexical matches found", outcome.model_text)
        self.assertNotIn("leak.py", outcome.model_text)

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
        self.assertIn("symlink paths are not supported", outcome.model_text)
        self.assertNotIn("target.py:1", outcome.model_text)

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
        self.assertIn("symlink paths are not supported", outcome.model_text)
        self.assertNotIn("target.py:1", outcome.model_text)

    def test_find_references_validates_simple_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            outcome = find_references(Path(td), ".", "foo.bar")

        self.assertFalse(outcome.ok)
        self.assertIn("requires a simple symbol", outcome.model_text)

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
        self.assertIn('near literal "request_timeout"', outcome.model_text)
        self.assertIn(
            "2 |     timeout = settings.get('request_timeout', 10)  # seconds",
            outcome.model_text,
        )

    def test_missing_reliable_anchor_returns_plain_error_without_file_start(self) -> None:
        content = "SECRET_FILE_START\nother = 2\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("x = 1", "x = 2")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("SEARCH text not found", outcome.model_text)
        self.assertIn("Use read_file", outcome.model_text)
        self.assertNotIn("SECRET_FILE_START", outcome.model_text)
        self.assertNotIn("Current bounded context", outcome.model_text)

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
        self.assertNotIn("Current bounded context", outcome.model_text)
        self.assertNotIn("super_user_id", outcome.model_text)

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
        self.assertIn('near identifier "user_id"', outcome.model_text)
        self.assertIn("2 | user_id = current_value", outcome.model_text)

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
        self.assertIn('near literal "user_id"', outcome.model_text)
        self.assertIn("super_user_id", outcome.model_text)

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
        self.assertIn("matched 3 times", outcome.model_text)
        self.assertIn("Exact matches start at lines: 1, 3, 5.", outcome.model_text)
        self.assertNotIn("Additional matches omitted", outcome.model_text)

    def test_additional_exact_matches_are_marked_omitted(self) -> None:
        content = "target()\n" * 5
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text(content, encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("target()", "changed()")])

            self.assertEqual(path.read_text(encoding="utf-8"), content)
        self.assertIn("Exact matches start at lines: 1, 2, 3.", outcome.model_text)
        self.assertIn("Additional matches omitted.", outcome.model_text)
        self.assertNotIn("1, 2, 3, 4", outcome.model_text)

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
        self.assertIn("Line 2 omitted", outcome.model_text)
        self.assertIn("use read_file offset=2 limit=1", outcome.model_text)
        self.assertNotIn("x" * EDIT_FAILURE_MAX_LINE_CHARS, outcome.model_text)

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
        self.assertLessEqual(len(outcome.model_text), EDIT_FAILURE_MAX_CHARS)
        for rendered_line in outcome.model_text.splitlines():
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
        self.assertIn("Exact matches start at lines: 2, 4.", outcome.model_text)

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
        self.assertIn("Replacement 2 of 3 failed. No replacements were written.", outcome.model_text)
        self.assertNotIn("Current bounded context", outcome.model_text)

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
        self.assertIn("matched 2 times", outcome.model_text)
        self.assertIn("Replacement 2 of 2 failed. No replacements were written.", outcome.model_text)
        self.assertNotIn("Exact matches start at lines", outcome.model_text)

    def test_successful_edit_output_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("old", "new")])

        self.assertEqual(outcome.model_text, "edited app.py (1 replacement)")

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
        self.assertIn("Syntax regression detected in app.py at line 1", outcome.model_text)
        self.assertIn("The edit was applied", outcome.model_text)

    def test_python_syntax_regression_is_reported_for_new_content_write(self) -> None:
        broken = "def value():\nreturn 1\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"

            outcome = write_file(root, "app.py", broken)

            self.assertEqual(path.read_text(encoding="utf-8"), broken)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.changed)
        self.assertIn("wrote app.py", outcome.model_text)
        self.assertIn("Syntax regression detected in app.py at line 2", outcome.model_text)

    def test_valid_python_edit_keeps_success_output_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("VALUE = 1\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("VALUE = 1", "VALUE = 2")])

        self.assertEqual(outcome.model_text, "edited app.py (1 replacement)")
        self.assertNotIn("Syntax regression", outcome.model_text)

    def test_existing_invalid_python_does_not_report_regression(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("def value()\n    return 1\n", encoding="utf-8")

            outcome = edit_file(root, "app.py", [EditBlock("return 1", "return 2")])

        self.assertTrue(outcome.ok)
        self.assertNotIn("Syntax regression", outcome.model_text)

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
        self.assertNotIn("Syntax regression", outcome.model_text)

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
        self.assertNotIn("Syntax regression", outcome.model_text)

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

        self.assertIn("Syntax regression", outcome.model_text)
        self.assertNotIn("private_customer_token", outcome.model_text)

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
                self.assertEqual(outcome.model_text, "edited app.py (1 replacement)")
                self.assertNotIn("Syntax regression", outcome.model_text)

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
        self.assertIn("1 | [tool_result] protocol_anchor current", outcome.model_text)

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
        self.assertIn(f"at most {MAX_REPLACEMENTS}", outcome.model_text)
        self.assertEqual(remaining, original)


if __name__ == "__main__":
    unittest.main()
