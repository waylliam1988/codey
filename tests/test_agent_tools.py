from __future__ import annotations

import unittest

from codey.agent_tools import DEFAULT_TOOL_FNS, AgentToolFns
from codey import tool_runtime


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
        def fake_read(*_args, **_kwargs):
            return tool_runtime.ToolOutcome("fake", True)

        fns = AgentToolFns(read_file=fake_read)
        self.assertIs(fns.read_file, fake_read)
        self.assertIs(fns.search_files, tool_runtime.search_files)

    def test_execute_run_command_uses_context_hook_when_present(self) -> None:
        seen = []

        def run_with_context(_root, rel, command, tool_id):
            seen.append((rel, command, tool_id))
            return tool_runtime.ToolOutcome("ok", True, exit_code=0)

        fns = AgentToolFns(run_command_with_context=run_with_context)
        outcome = fns.execute_run_command(
            None,
            ".",
            "python -m pytest -q",
            tool_id="2:1",
        )

        self.assertTrue(outcome.ok)
        self.assertEqual(seen, [(".", "python -m pytest -q", "2:1")])


if __name__ == "__main__":
    unittest.main()
