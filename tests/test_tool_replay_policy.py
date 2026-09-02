"""Deterministic tests for tool and runtime effect replay policy."""

from __future__ import annotations

import unittest

from codey.runtime.replay_policy import (
    REPLAYABLE_SAFE_TOOL_NAMES,
    ReplayClass,
    SAFE_RUNTIME_TOOL_NAMES,
    UNSAFE_RUNTIME_TOOL_NAMES,
    is_replayable_safe_tool,
    provider_replay_policy,
    repair_replay_policy,
    tool_replay_policy,
)
from codey.runtime.replay_args import REPLAY_ARG_TOOL_NAMES
from codey.toolchain.definition import (
    READ_ONLY_RUNTIME_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
)


class ToolReplayPolicyTests(unittest.TestCase):
    def test_safe_tools_are_classified_as_safe(self) -> None:
        for tool_name in SAFE_RUNTIME_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.SAFE)
                self.assertEqual(decision.reason, "read_only_tool")

    def test_replayable_safe_tools_narrow_whitelist(self) -> None:
        self.assertEqual(REPLAYABLE_SAFE_TOOL_NAMES, frozenset({"read", "ls", "search", "references"}))
        for tool in ("read", "ls", "search", "references"):
            self.assertTrue(is_replayable_safe_tool(tool))
        # Context/research names are not coding runtime tools.
        self.assertFalse(is_replayable_safe_tool("project_facts"))
        self.assertFalse(is_replayable_safe_tool("project_map"))
        self.assertFalse(is_replayable_safe_tool("knowledge_write"))
        for tool in UNSAFE_RUNTIME_TOOL_NAMES:
            self.assertFalse(is_replayable_safe_tool(tool))

    def test_unsafe_tools_are_classified_as_unsafe(self) -> None:
        for tool_name in UNSAFE_RUNTIME_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)

    def test_run_command_is_unconditionally_unsafe(self) -> None:
        decision = tool_replay_policy("run")
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertEqual(decision.reason, "mutating_or_executing_tool")

    def test_unknown_tool_defaults_to_unsafe(self) -> None:
        unknown_tools = [
            "weird_plugin",
            "custom_exec",
            "rm_rf",
            "project_facts",
            "project_map",
            "write",
            "knowledge_write",
            "",
        ]
        for tool_name in unknown_tools:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
                self.assertEqual(decision.reason, "unknown_tool")

    def test_runtime_tool_policy_invariants(self) -> None:
        self.assertEqual(SAFE_RUNTIME_TOOL_NAMES, READ_ONLY_RUNTIME_TOOL_NAMES)
        self.assertEqual(
            UNSAFE_RUNTIME_TOOL_NAMES,
            SUPPORTED_RUNTIME_TOOL_NAMES - SAFE_RUNTIME_TOOL_NAMES,
        )
        self.assertEqual(REPLAYABLE_SAFE_TOOL_NAMES, REPLAY_ARG_TOOL_NAMES)
        self.assertEqual(
            SUPPORTED_RUNTIME_TOOL_NAMES - SAFE_RUNTIME_TOOL_NAMES - UNSAFE_RUNTIME_TOOL_NAMES,
            frozenset(),
        )

    def test_policy_denied_overrides_to_unsafe_denied(self) -> None:
        decision = tool_replay_policy("read", policy_denied=True)
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertEqual(decision.reason, "policy_denied")

    def test_approval_required_marks_decision(self) -> None:
        decision = tool_replay_policy("shell", approval_required=True)
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertEqual(decision.reason, "approval_required")

    def test_provider_and_repair_replay_policies_are_unsafe(self) -> None:
        p_decision = provider_replay_policy("coding_prompt")
        self.assertEqual(p_decision.replay_class, ReplayClass.UNSAFE)
        self.assertEqual(p_decision.reason, "outbound_provider_call")

        r_decision = repair_replay_policy()
        self.assertEqual(r_decision.replay_class, ReplayClass.UNSAFE)
        self.assertEqual(r_decision.reason, "completion_repair_round")


if __name__ == "__main__":
    unittest.main()
