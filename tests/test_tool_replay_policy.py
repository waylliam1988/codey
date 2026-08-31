"""Deterministic tests for tool and runtime effect replay policy."""

from __future__ import annotations

import unittest

from codey.runtime.replay_policy import (
    ReplayClass,
    provider_replay_policy,
    repair_replay_policy,
    tool_replay_policy,
)


class ToolReplayPolicyTests(unittest.TestCase):
    def test_safe_tools_are_classified_as_safe_and_retryable(self) -> None:
        safe_tools = ["read", "ls", "search", "references", "project_facts", "project_map"]
        for tool_name in safe_tools:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.SAFE)
                self.assertTrue(decision.retryable)
                self.assertFalse(decision.policy_denied)
                self.assertFalse(decision.approval_required)

    def test_unsafe_tools_are_classified_as_unsafe_and_not_retryable(self) -> None:
        unsafe_tools = ["edit", "write", "shell", "run", "knowledge_write"]
        for tool_name in unsafe_tools:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
                self.assertFalse(decision.retryable)

    def test_run_command_is_unconditionally_unsafe(self) -> None:
        decision = tool_replay_policy("run")
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertFalse(decision.retryable)

    def test_unknown_tool_defaults_to_unsafe(self) -> None:
        unknown_tools = ["weird_plugin", "custom_exec", "rm_rf", ""]
        for tool_name in unknown_tools:
            with self.subTest(tool=tool_name):
                decision = tool_replay_policy(tool_name)
                self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
                self.assertFalse(decision.retryable)
                self.assertEqual(decision.reason, "unknown_tool")

    def test_policy_denied_overrides_to_unsafe_denied(self) -> None:
        decision = tool_replay_policy("read", policy_denied=True)
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertFalse(decision.retryable)
        self.assertTrue(decision.policy_denied)
        self.assertEqual(decision.reason, "policy_denied")

    def test_approval_required_marks_decision(self) -> None:
        decision = tool_replay_policy("shell", approval_required=True)
        self.assertEqual(decision.replay_class, ReplayClass.UNSAFE)
        self.assertFalse(decision.retryable)
        self.assertTrue(decision.approval_required)
        self.assertEqual(decision.reason, "approval_required")

    def test_provider_and_repair_replay_policies_are_unsafe(self) -> None:
        p_decision = provider_replay_policy("coding_prompt")
        self.assertEqual(p_decision.replay_class, ReplayClass.UNSAFE)
        self.assertFalse(p_decision.retryable)

        r_decision = repair_replay_policy()
        self.assertEqual(r_decision.replay_class, ReplayClass.UNSAFE)
        self.assertFalse(r_decision.retryable)


if __name__ == "__main__":
    unittest.main()
