from __future__ import annotations

import unittest
from pathlib import Path
from typing import Unpack

from codey.agents.tool_execution import execute_information_tool_call
from codey.agents.tools import DEFAULT_TOOL_FNS, AgentToolFns, ReadOptions, SearchOptions, search_call_options
from codey.runtime.core.models import ToolCall
from codey.toolchain import runtime as tool_runtime
from codey.types.json_value import JsonObject


class AgentToolFnsTests(unittest.TestCase):
    def test_defaults_bind_to_runtime_tools(self) -> None:
        self.assertIs(DEFAULT_TOOL_FNS.read_file, tool_runtime.read_file)
        self.assertIs(DEFAULT_TOOL_FNS.list_directory, tool_runtime.list_directory)
        self.assertIs(DEFAULT_TOOL_FNS.search_files, tool_runtime.search_files)
        self.assertIs(DEFAULT_TOOL_FNS.find_references, tool_runtime.find_references)
        self.assertIs(DEFAULT_TOOL_FNS.write_file, tool_runtime.write_file)
        self.assertIs(DEFAULT_TOOL_FNS.edit_file, tool_runtime.edit_file)
        self.assertIs(DEFAULT_TOOL_FNS.run_command, tool_runtime.run_command)

    def test_injection_replaces_only_supplied_tools(self) -> None:
        def fake_read(
            root: Path, rel: str, *, offset: int = 1, limit: int = tool_runtime.READ_DEFAULT_LINES,
        ) -> tool_runtime.ToolOutcome:
            return tool_runtime.ToolOutcome("fake", True)

        fns = AgentToolFns(read_file=fake_read)
        self.assertIs(fns.read_file, fake_read)
        self.assertIs(fns.search_files, tool_runtime.search_files)

    def test_execute_run_command_uses_context_hook_when_present(self) -> None:
        seen: list[tuple[str, str, str, str, str]] = []

        def run_with_context(
            root: Path, rel: str, command: str, tool_id: str, permission_profile: str, phase: str,
        ) -> tool_runtime.ToolOutcome:
            seen.append((rel, command, tool_id, permission_profile, phase))
            return tool_runtime.ToolOutcome("ok", True, exit_code=0)

        fns = AgentToolFns(run_command_with_context=run_with_context)
        outcome = fns.execute_run_command(
            Path("."),
            ".",
            "python -m pytest -q",
            permission_profile="coding_writer",
            tool_id="2:1",
        )

        self.assertTrue(outcome.ok)
        expected: list[tuple[str, str, str, str, str]] = [
            (".", "python -m pytest -q", "2:1", "coding_writer", "writer"),
        ]
        self.assertEqual(seen, expected)

    def test_information_dispatch_preserves_optional_keyword_presence(self) -> None:
        seen: list[ReadOptions] = []

        def read(root: Path, rel: str, **options: Unpack[ReadOptions]) -> tool_runtime.ToolOutcome:
            seen.append(options)
            return tool_runtime.ToolOutcome("fake", True)

        fns = AgentToolFns(read_file=read)
        calls: tuple[JsonObject, ...] = ({"path": "note.py"}, {"path": "note.py", "offset": 2, "limit": 3})
        for args in calls:
            outcome = execute_information_tool_call(Path("."), fns, ToolCall("read", args))
            self.assertTrue(outcome.ok)
        expected: list[ReadOptions] = [ReadOptions(), ReadOptions(offset=2, limit=3)]
        self.assertEqual(seen, expected)

    def test_search_options_reject_values_outside_the_frozen_field_types(self) -> None:
        invalid: tuple[JsonObject, ...] = (
            {"offset": True}, {"limit": "2"}, {"literal": 1}, {"case_sensitive": "false"},
            {"include": []}, {"exclude": "*.py"}, {"exclude": [1]},
            {"before_context": 1.5}, {"after_context": "1"},
        )
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                search_call_options(args)

    def test_search_options_keep_explicit_false_zero_and_empty_values(self) -> None:
        args: JsonObject = {
            "path": "src", "literal": False, "case_sensitive": True, "include": "",
            "exclude": [], "before_context": 0, "after_context": 1,
        }
        actual = search_call_options(args)
        expected: SearchOptions = {
            "literal": False, "case_sensitive": True, "include": "", "exclude": [],
            "before_context": 0, "after_context": 1,
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
