"""原生混合批次必须显式拒绝：有 ID 与无 ID 不得混送。

kernel_transport._native_tool_messages 遇到混合批次抛 ValueError，
task_loop 终止为 protocol，不回退文本。跨提供者文本交付只发生在
明确的 provider_session_changed 分支。
"""
from __future__ import annotations

import unittest


class MixedCallIdBatchTests(unittest.TestCase):
    def test_mixed_call_id_batch_is_explicitly_rejected(self) -> None:
        from codey.operations import kernel_transport as t
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        mixed = [
            ToolResult(call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"), model_text="a"),
            ToolResult(call=ToolCall(name="read_file", args={"path": "b.py"}, call_id=""), model_text="b"),
        ]
        with self.assertRaises(ValueError):
            t._native_tool_messages(mixed, session)

    def test_all_idless_returns_empty_for_text_fallback(self) -> None:
        from codey.operations import kernel_transport as t
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        idless = [
            ToolResult(call=ToolCall(name="read_file", args={"path": "a.py"}, call_id=""), model_text="a"),
        ]
        self.assertEqual(t._native_tool_messages(idless, session), [])


if __name__ == "__main__":
    unittest.main()
