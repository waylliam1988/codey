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

    def test_kernel_stops_protocol_and_preserves_receipts_on_mixed_batch(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest import mock

        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.providers.base import AssistantTurn, ProviderToolCall
        from codey.runtime.core.models import ToolResult

        class _SingleReadProvider:
            name = "local"

            def __init__(self, turns: list) -> None:
                self._turns = list(turns)

            def new_chat(self, timeout=None) -> None:
                return None

            def send(self, text: str, timeout=None) -> str:
                raise AssertionError("native path must not use text send()")

            def send_turn(self, prompt: str, tools=None, timeout=None):
                return self._turns.pop(0)

            def send_tool_results(self, results, tools=None, timeout=None):
                raise AssertionError("mixed batch must stop before delivery")

            def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("hello\n", encoding="utf-8")
            provider = _SingleReadProvider(
                [
                    AssistantTurn(
                        text="",
                        tool_calls=(
                            ProviderToolCall(id="c1", name="read_file", arguments={"path": "a.py"}),
                        ),
                    ),
                ]
            )
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.read", "control"})),
                task_kind="project",
                project=str(project),
                max_turns=2,
            )
            with (
                mock.patch(
                    "codey.operations.kernel_transport.provider_uses_native", return_value=True
                ),
                mock.patch(
                    "codey.operations.kernel_transport._native_tool_messages",
                    side_effect=ValueError("mixed native batch: test"),
                ),
            ):
                result = run_task_kernel(
                    session,
                    provider=provider,
                    executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
                    run_id="r-mixed-kernel-1",
                    effect_scope="task",
                    provider_id="local",
                    project_path=project,
                    user_task="read",
                    context_text="",
                )
            self.assertFalse(result.completed)
            self.assertEqual(result.stop_reason, "protocol")
            self.assertIn("mixed", result.summary.lower())
            # Executed receipts are kept even though the native chain stops.
            self.assertTrue(session.executed, "tool receipts must be preserved")

    def test_custom_executor_call_mismatch_reuses_original_call_id(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("hello\n", encoding="utf-8")
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.read", "control"})),
                task_kind="project",
                project=str(project),
                max_turns=2,
            )

            def _rogue(call: ToolCall):
                return ToolResult(
                    call=ToolCall(name="read_file", args={"path": "other.py"}, call_id="rogue-9"),
                    model_text="rogue",
                )

            results = execute_turn(
                session,
                [ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")],
                executors={"read_file": _rogue},
                run_id="r-rogue-1",
                turn=1,
                project_path=None,
                tool_fns=None,
                research_tools=None,
            )
            self.assertEqual(str(results[0].call.call_id or ""), "c1")
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
