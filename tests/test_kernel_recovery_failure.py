"""恢复失败必须显式终止：格式化异常不得静默丢弃待恢复结果。

生产链：kernel_recovery.apply_recovery_first 抛 RecoveryFailed
  -> task_loop.run_task_kernel 返回 recovery_failure，不发初始 prompt，
     不重执行工具，原收据保留。
"""
from __future__ import annotations

import unittest

from codey.operations.task_loop import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps


class RecoveryFormatErrorTests(unittest.TestCase):
    def test_recovery_format_error_must_raise_not_drop(self) -> None:
        from codey.operations.kernel_recovery import apply_recovery_first
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        pending = [
            ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}), model_text="content")
        ]

        def boom(rows, _sess):
            raise RuntimeError("format boom")

        with self.assertRaises(RuntimeError):
            apply_recovery_first(
                session, False, pending, "ORIGINAL", None,
                provider_session_changed=False,
                format_results=boom,
                native_tool_messages=lambda rows, _s: [],
            )

    def test_run_task_kernel_recovery_failure_never_sends_initial_prompt(self) -> None:
        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        session.executed["k1"] = {"name": "read_file", "ok": True, "call_id": "", "excerpt": "old", "args_digest": "d"}

        class NoCallProvider:
            def __init__(self):
                self.calls = 0

            def send(self, prompt, timeout=None):
                self.calls += 1
                return '{"done": {"summary": "x"}}'

        provider = NoCallProvider()
        pending = [
            ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}), model_text="content")
        ]
        import codey.operations.kernel_prompt as kp

        orig = kp._format_results if hasattr(kp, "_format_results") else None
        executed: list[str] = []

        def boom_format(rows, _sess):
            raise RuntimeError("format boom")

        def fake_tool(call):
            executed.append(str(getattr(call, "name", "")))
            return ToolResult(ok=True, call=call, model_text="re-executed")

        try:
            if orig is not None:
                kp._format_results = boom_format  # type: ignore[attr-defined]
            result = run_task_kernel(
                session,
                request=KernelRunRequest(
                    transport=KernelTransportDeps(
                        provider=provider,
                        run_id="r-rec",
                        effect_scope="task",
                        user_task="t",
                        context_text="",
                        initial_results=pending,
                    ),
                    execution=KernelExecutionDeps(
                        executors={"read_file": fake_tool},
                    ),
                ),
            )
        finally:
            if orig is not None:
                kp._format_results = orig  # type: ignore[attr-defined]
        self.assertEqual(provider.calls, 0, "recovery failure must not send initial prompt")
        self.assertEqual(executed, [], "recovery failure must not re-execute tools")
        self.assertIn("k1", session.executed, "original receipts must survive recovery failure")
        self.assertFalse(result.completed)
        self.assertEqual(result.stop_reason, "recovery_failure")


if __name__ == "__main__":
    unittest.main()
