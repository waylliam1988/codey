"""Tests for shared tool argument canonicalization and repair."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from codey.tool_args_repair import (
    ToolArgLimits,
    ToolArgsRepairError,
    ToolArgsRepairResult,
    normalize_tool_args,
)


class ToolArgsRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = ToolArgLimits(
            max_replacements=8,
            read_max_lines=600,
        )

    def test_pure_function_has_no_internal_subpackage_dependencies(self) -> None:
        path = Path(__file__).resolve().parents[1] / "codey" / "tool_args_repair.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden_prefixes = (
            "codey.runtime",
            "codey.agents",
            "codey.providers",
            "codey.operations",
            "codey.ghost",
            "codey.toolchain",
            "codey.policies",
            "codey.workspace",
        )
        for mod in imported:
            for prefix in forbidden_prefixes:
                self.assertFalse(
                    mod == prefix or mod.startswith(f"{prefix}."),
                    f"tool_args_repair.py should not import {mod}",
                )

    def test_path_normalization_converts_backslashes_and_folds_dots(self) -> None:
        res = normalize_tool_args("read", {"path": r"src\components\app.py"}, limits=self.limits)
        self.assertIsInstance(res, ToolArgsRepairResult)
        self.assertEqual(res.args["path"], "src/components/app.py")
        self.assertGreater(res.alias_rewrite_count, 0)
        self.assertIn("path_normalized", res.arg_repair_counts)

        res_dots = normalize_tool_args("read", {"path": "src/./components/../components/app.py"}, limits=self.limits)
        self.assertEqual(res_dots.args["path"], "src/components/app.py")

    def test_path_normalization_rejects_drive_and_unc_paths(self) -> None:
        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("read", {"path": r"C:\Windows\System32"}, limits=self.limits)
        self.assertIn("absolute drive paths", str(ctx.exception))

        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("read", {"path": r"\\server\share\file.txt"}, limits=self.limits)
        self.assertIn("UNC paths", str(ctx.exception))

        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("read", {"path": "/etc/passwd"}, limits=self.limits)
        self.assertIn("absolute paths", str(ctx.exception))

    def test_path_normalization_rejects_parent_traversal_escape(self) -> None:
        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("read", {"path": "../outside.py"}, limits=self.limits)
        self.assertIn("parent directory traversal", str(ctx.exception))

        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("read", {"path": "src/../../outside.py"}, limits=self.limits)
        self.assertIn("parent directory traversal", str(ctx.exception))

    def test_cwd_alias_is_rewritten_to_path(self) -> None:
        res = normalize_tool_args("read", {"cwd": "app.py"}, limits=self.limits)
        self.assertEqual(res.args["path"], "app.py")
        self.assertNotIn("cwd", res.args)
        self.assertIn("path_alias", res.arg_repair_counts)

    def test_read_numeric_string_coercion(self) -> None:
        res = normalize_tool_args("read", {"path": "app.py", "offset": "10", "limit": "50"}, limits=self.limits)
        self.assertEqual(res.args["offset"], 10)
        self.assertEqual(res.args["limit"], 50)
        self.assertEqual(res.arg_repair_counts.get("numeric_coerced"), 2)

    def test_read_rejects_invalid_numbers(self) -> None:
        for invalid in (True, False, 0, -1, "0", "-5", 1.5, "abc"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("read", {"path": "app.py", "offset": invalid}, limits=self.limits)

    def test_read_rejects_explicit_null_numbers(self) -> None:
        for name in ("offset", "limit"):
            with self.subTest(name=name):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("read", {"path": "app.py", name: None}, limits=self.limits)

    def test_read_rejects_limit_exceeding_max(self) -> None:
        with self.assertRaises(ToolArgsRepairError):
            normalize_tool_args("read", {"path": "app.py", "limit": self.limits.read_max_lines + 1}, limits=self.limits)

    def test_edit_old_and_new_field_aliases_in_single_mode(self) -> None:
        cases = (
            ({"path": "a.py", "old": "x", "new": "y"}, ("x", "y")),
            ({"path": "a.py", "search": "x", "replace": "y"}, ("x", "y")),
            ({"path": "a.py", "before": "x", "after": "y"}, ("x", "y")),
            ({"path": "a.py", "old_string": "x", "replacement": "y"}, ("x", "y")),
        )
        for args, (expected_search, expected_replace) in cases:
            with self.subTest(args=args):
                res = normalize_tool_args("edit", args, limits=self.limits)
                self.assertEqual(
                    res.args["replacements"],
                    [{"search": expected_search, "replace": expected_replace}],
                )
                self.assertGreater(res.alias_rewrite_count, 0)

    def test_edit_missing_new_string_fails_closed(self) -> None:
        cases = (
            {"path": "a.py", "old_string": "x"},
            {"path": "a.py", "replacements": [{"old_string": "x"}]},
        )
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("edit", args, limits=self.limits)

    def test_edit_explicit_empty_new_string_aliases_are_deletions(self) -> None:
        cases = (
            {"path": "a.py", "old_string": "x", "new_string": ""},
            {"path": "a.py", "old_string": "x", "replace": ""},
            {"path": "a.py", "old_string": "x", "after": ""},
            {"path": "a.py", "old_string": "x", "new": ""},
            {"path": "a.py", "replacements": [{"old_string": "x", "new_string": ""}]},
        )
        for args in cases:
            with self.subTest(args=args):
                res = normalize_tool_args("edit", args, limits=self.limits)
                self.assertEqual(res.args["replacements"], [{"search": "x", "replace": ""}])

    def test_edit_json_string_replacements(self) -> None:
        raw_json = json.dumps([{"old_string": "foo", "new_string": "bar"}])
        res = normalize_tool_args("edit", {"path": "a.py", "replacements": raw_json}, limits=self.limits)
        self.assertEqual(res.args["replacements"], [{"search": "foo", "replace": "bar"}])
        self.assertIn("json_replacements_parsed", res.arg_repair_counts)

    def test_edit_invalid_json_string_replacements_fails_closed(self) -> None:
        with self.assertRaises(ToolArgsRepairError) as ctx:
            normalize_tool_args("edit", {"path": "a.py", "replacements": "{invalid json"}, limits=self.limits)
        self.assertIn("could not be parsed", str(ctx.exception))

    def test_edit_single_dict_wrapped_in_replacements(self) -> None:
        res = normalize_tool_args(
            "edit",
            {"path": "a.py", "replacements": {"old_string": "foo", "new_string": "bar"}},
            limits=self.limits,
        )
        self.assertEqual(res.args["replacements"], [{"search": "foo", "replace": "bar"}])
        self.assertIn("replacement_object_wrapped", res.arg_repair_counts)

    def test_edit_mode_conflict_rejects(self) -> None:
        with self.assertRaises(ToolArgsRepairError):
            normalize_tool_args(
                "edit",
                {"path": "a.py", "content": "hello", "old_string": "foo", "new_string": "bar"},
                limits=self.limits,
            )

    def test_search_pattern_alias_and_missing_query(self) -> None:
        res = normalize_tool_args("search", {"pattern": "needle", "path": "src"}, limits=self.limits)
        self.assertEqual(res.args["query"], "needle")
        self.assertEqual(res.args["path"], "src")
        self.assertIn("search_field_alias", res.arg_repair_counts)

        with self.assertRaises(ToolArgsRepairError):
            normalize_tool_args("search", {"path": "src"}, limits=self.limits)

    def test_references_name_alias_and_missing_symbol(self) -> None:
        res = normalize_tool_args("references", {"name": "MyClass"}, limits=self.limits)
        self.assertEqual(res.args["symbol"], "MyClass")
        self.assertEqual(res.args["path"], ".")
        self.assertIn("references_field_alias", res.arg_repair_counts)

        with self.assertRaises(ToolArgsRepairError):
            normalize_tool_args("references", {"path": "."}, limits=self.limits)

    def test_run_cmd_alias_and_missing_command(self) -> None:
        res = normalize_tool_args("run", {"cmd": "pytest -q"}, limits=self.limits)
        self.assertEqual(res.args["command"], "pytest -q")
        self.assertEqual(res.args["path"], ".")
        self.assertIn("command_field_alias", res.arg_repair_counts)

        with self.assertRaises(ToolArgsRepairError):
            normalize_tool_args("run", {"path": "."}, limits=self.limits)


    def test_path_normalization_preserves_internal_spaces_in_segments(self) -> None:
        res = normalize_tool_args("read", {"path": "dir / file.py"}, limits=self.limits)
        self.assertEqual(res.args["path"], "dir / file.py")

    def test_edit_content_rejects_non_string_types(self) -> None:
        for invalid_content in (0, False, None, [1, 2], {"a": 1}):
            with self.subTest(invalid_content=invalid_content):
                with self.assertRaises(ToolArgsRepairError) as ctx:
                    normalize_tool_args("edit", {"path": "a.py", "content": invalid_content}, limits=self.limits)
                self.assertIn("content must be a string", str(ctx.exception))

    def test_unsupported_runtime_tool_fails_closed(self) -> None:
        for unsupported in ("future_tool", "write_file", "unknown_xyz", "browser"):
            with self.subTest(tool=unsupported):
                with self.assertRaises(ToolArgsRepairError) as ctx:
                    normalize_tool_args(unsupported, {"path": "a.py"}, limits=self.limits)
                self.assertIn("unsupported runtime tool", str(ctx.exception))

    def test_text_args_reject_non_string_types_and_blanks(self) -> None:
        invalid_values = (0, False, None, [], {}, "", "   ")
        for inv in invalid_values:
            with self.subTest(tool="search", val=inv):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("search", {"query": inv, "path": "."}, limits=self.limits)
            with self.subTest(tool="references", val=inv):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("references", {"symbol": inv, "path": "."}, limits=self.limits)
            with self.subTest(tool="run", val=inv):
                with self.assertRaises(ToolArgsRepairError):
                    normalize_tool_args("run", {"command": inv, "path": "."}, limits=self.limits)

    def test_text_args_preserve_explicit_non_blank_strings(self) -> None:
        search = normalize_tool_args("search", {"query": " needle ", "path": "."}, limits=self.limits)
        references = normalize_tool_args("references", {"symbol": " Thing ", "path": "."}, limits=self.limits)
        run = normalize_tool_args("run", {"command": " python -m pytest -q ", "path": "."}, limits=self.limits)

        self.assertEqual(search.args["query"], " needle ")
        self.assertEqual(references.args["symbol"], " Thing ")
        self.assertEqual(run.args["command"], " python -m pytest -q ")

    def test_conflicting_alias_fields_fail_closed(self) -> None:
        conflict_cases = (
            ("search", {"query": "foo", "pattern": "bar", "path": "."}),
            ("references", {"symbol": "SymA", "name": "SymB", "path": "."}),
            ("run", {"command": "pytest", "cmd": "rm -rf x", "path": "."}),
            ("read", {"path": "a.py", "cwd": "b.py"}),
            ("edit", {"path": "a.py", "old_string": "foo", "old": "bar", "new_string": "baz"}),
            ("edit", {"path": "a.py", "old_string": "foo", "new_string": "baz", "replace": "qux"}),
        )
        for tool, args in conflict_cases:
            with self.subTest(tool=tool, args=args):
                with self.assertRaises(ToolArgsRepairError) as ctx:
                    normalize_tool_args(tool, args, limits=self.limits)
                self.assertIn("conflicting", str(ctx.exception))

    def test_optional_paths_default_only_when_missing(self) -> None:
        defaults = (
            ("ls", {}),
            ("search", {"query": "needle"}),
            ("references", {"symbol": "Thing"}),
            ("run", {"command": "python -m pytest -q"}),
        )
        for tool, args in defaults:
            with self.subTest(tool=tool):
                res = normalize_tool_args(tool, args, limits=self.limits)
                self.assertEqual(res.args["path"], ".")

        for tool, args in defaults:
            for bad_path in ("", "   ", None):
                with self.subTest(tool=tool, bad_path=bad_path):
                    with self.assertRaises(ToolArgsRepairError):
                        normalize_tool_args(tool, {**args, "path": bad_path}, limits=self.limits)

    def test_unknown_argument_fields_fail_closed(self) -> None:
        cases = (
            ("read", {"path": "app.py", "extra": "ignored"}),
            ("ls", {"path": ".", "extra": "ignored"}),
            ("search", {"query": "needle", "path": ".", "extra": "ignored"}),
            ("references", {"symbol": "Thing", "path": ".", "extra": "ignored"}),
            ("run", {"command": "python -m pytest -q", "path": ".", "extra": "ignored"}),
            ("edit", {"path": "a.py", "old_string": "x", "new_string": "y", "extra": "ignored"}),
            (
                "edit",
                {
                    "path": "a.py",
                    "replacements": [
                        {"old_string": "x", "new_string": "y", "extra": "ignored"}
                    ],
                },
            ),
        )
        for tool, args in cases:
            with self.subTest(tool=tool, args=args):
                with self.assertRaises(ToolArgsRepairError) as ctx:
                    normalize_tool_args(tool, args, limits=self.limits)
                self.assertIn("unsupported fields", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
