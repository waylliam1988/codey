"""Shared call_arg helper must stay single-sourced and deterministic."""

from __future__ import annotations

import unittest

from codey.agents import tool_execution as tool_execution_module
from codey.runtime.core.models import ToolCall
from codey.toolchain import definition as definition_module


class CallArgSharedTests(unittest.TestCase):
    def test_both_helpers_agree_on_edge_cases(self) -> None:
        cases = [
            (ToolCall("read", {"path": "a.py"}), "path", "", "a.py"),
            (ToolCall("read", {}), "path", ".", "."),
            (ToolCall("read", {"path": None}), "path", ".", "."),
            (ToolCall("run", {"command": 123}), "command", "", "123"),
            (ToolCall("run", {"command": ""}), "command", "fallback", ""),
        ]
        for call, name, default, expected in cases:
            with self.subTest(call=call, name=name):
                self.assertEqual(tool_execution_module.call_arg(call, name, default), expected)
                self.assertEqual(definition_module.call_arg(call, name, default), expected)

    def test_tool_execution_reuses_definition_helper(self) -> None:
        self.assertIs(tool_execution_module.call_arg, definition_module.call_arg)




if __name__ == "__main__":
    unittest.main()
