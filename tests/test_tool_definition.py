from __future__ import annotations

import json
import unittest

from codey.runtime.models import ToolCall
from codey.protocols import JsonToolCodec
from codey.toolchain.definition import (
    INFORMATION_RUNTIME_TOOL_NAMES,
    READ_ONLY_RUNTIME_TOOL_NAMES,
    RESULT_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
    TOOL_DEFINITIONS,
    TOOL_DEFINITION_BY_NAME,
    definitions_for_permissions,
    definitions_for_tool_names,
    public_example,
    render_tool_activity,
    render_tool_contract,
)


class ToolDefinitionTests(unittest.TestCase):
    def test_public_tool_names_are_the_coding_contract(self) -> None:
        self.assertEqual(
            tuple(definition.name for definition in TOOL_DEFINITIONS),
            (
                "list_dir",
                "read_file",
                "read_files",
                "grep",
                "find_references",
                "parallel",
                "edit",
                "run",
                "shell",
                "done",
            ),
        )
        self.assertNotIn("write", TOOL_DEFINITION_BY_NAME)
        self.assertNotIn("write_file", TOOL_DEFINITION_BY_NAME)

    def test_definition_names_aliases_and_runtime_names_are_consistent(self) -> None:
        public_names = [
            name for definition in TOOL_DEFINITIONS for name in (definition.name, *definition.aliases)
        ]
        runtime_names = {
            definition.runtime_name
            for definition in TOOL_DEFINITIONS
            if definition.runtime_name is not None
        }

        self.assertEqual(len(public_names), len(set(public_names)))
        self.assertEqual(set(public_names), set(TOOL_DEFINITION_BY_NAME))
        self.assertEqual(
            runtime_names,
            {"edit", "ls", "read", "references", "run", "search", "shell"},
        )
        self.assertEqual(SUPPORTED_RUNTIME_TOOL_NAMES, runtime_names)
        self.assertEqual(set(RESULT_TOOL_NAMES), runtime_names)
        self.assertEqual(
            READ_ONLY_RUNTIME_TOOL_NAMES,
            {"ls", "read", "references", "search"},
        )
        self.assertEqual(
            INFORMATION_RUNTIME_TOOL_NAMES,
            {"ls", "read", "references", "run", "search", "shell"},
        )

    def test_definitions_are_metadata_not_runtime_validation(self) -> None:
        for definition in TOOL_DEFINITIONS:
            with self.subTest(tool=definition.name):
                self.assertTrue(definition.permission)
                self.assertTrue(definition.description)
                self.assertTrue(definition.render_hint)
                self.assertTrue(definition.repair_hint)
                self.assertFalse(definition.parallel_safe and not definition.read_only)
                for example in definition.examples:
                    plan = JsonToolCodec().parse(example)
                    self.assertEqual(plan.protocol_error, "")
                    self.assertTrue(plan.calls or plan.control is not None)

    def test_definition_filters_preserve_contract_order(self) -> None:
        readonly = definitions_for_tool_names(("grep", "read_file", "done"))
        writable = definitions_for_permissions(("project_write", "control"))

        self.assertEqual(
            tuple(definition.name for definition in readonly),
            ("read_file", "grep", "done"),
        )
        self.assertEqual(
            tuple(definition.name for definition in writable),
            ("edit", "done"),
        )
        self.assertIn('{"tool":"grep"', render_tool_contract(readonly))
        self.assertNotIn('{"tool":"edit"', render_tool_contract(readonly))
        self.assertEqual(render_tool_contract(()), "")
        self.assertEqual(public_example("edit", readonly), "")

    def test_write_file_remains_unknown_tool(self) -> None:
        plan = JsonToolCodec().parse(json.dumps({
            "tool": "write_file",
            "args": {"path": "app.py", "content": "VALUE = 1\n"},
        }))

        self.assertEqual(plan.protocol_error_kind, "unknown_tool")
        self.assertIn("Use edit with content", plan.protocol_error)

    def test_render_tool_activity_matches_existing_agent_rows(self) -> None:
        cases = (
            (ToolCall("read", {"path": "app.py"}), "Reading app.py"),
            (ToolCall("ls", {"path": "."}), "Listing ."),
            (
                ToolCall("search", {"path": "src", "query": "login handler"}),
                "Searching src for login handler",
            ),
            (
                ToolCall("references", {"symbol": "createRouter"}),
                "Finding references for createRouter",
            ),
            (ToolCall("edit", {"path": "app.py", "content": "x"}), "Writing app.py"),
            (ToolCall("edit", {"path": "app.py"}), "Editing app.py"),
            (
                ToolCall("run", {"command": "python -m pytest -q"}),
                "Running python -m pytest -q",
            ),
            (
                ToolCall("shell", {"command": "git status --short"}),
                "Requesting shell approval for git status --short",
            ),
            (ToolCall("unknown", {}), "Using unknown"),
        )

        for call, expected in cases:
            with self.subTest(call=call):
                self.assertEqual(render_tool_activity(call), expected)

    def test_output_facts_match_run_ledger_v1_fact_names(self) -> None:
        self.assertEqual(TOOL_DEFINITION_BY_NAME["read_file"].output_facts, ())
        self.assertEqual(TOOL_DEFINITION_BY_NAME["read_files"].output_facts, ())
        self.assertEqual(TOOL_DEFINITION_BY_NAME["grep"].output_facts, ())
        self.assertEqual(TOOL_DEFINITION_BY_NAME["find_references"].output_facts, ())
        self.assertEqual(
            TOOL_DEFINITION_BY_NAME["edit"].output_facts,
            ("file_changed",),
        )
        self.assertEqual(
            TOOL_DEFINITION_BY_NAME["run"].output_facts,
            ("command_verified",),
        )


if __name__ == "__main__":
    unittest.main()
