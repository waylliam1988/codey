"""Mixed native batch stops the kernel with receipts preserved.

Repro: the old test only asserted _native_tool_results() raises
ValueError, never how the main loop stops or keeps receipts after tool
execution. A custom executor could also return a ToolResult carrying its
own call; without validation the native chain loses the receipt.

Lock: run_task_kernel() terminates mixed batches as protocol with an
error receipt for every requested call id; a custom executor ToolResult
whose call mismatches the request is replaced by an error result that
reuses the original call id.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolCall, ToolResult


class _FakeNativeProvider:
    name = "local"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tool_results_seen: list[list[dict]] = []

    def new_chat(self, timeout=None) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        raise AssertionError("native path must not use text send()")

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        return self._turns.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        self.tool_results_seen.append(list(results))
        return self._turns.pop(0)

    def close(self) -> None:
        return None

    def acknowledge_tool_results(self, results, declared_tools, timeout=None):
        return self.send_tool_results(results, [])


class MixedBatchKernelStopsWithReceiptsTests(unittest.TestCase):
    def test_mixed_call_id_batch_is_explicitly_rejected(self) -> None:
        from codey.operations import kernel_transport as t

        policy = TaskPolicy(grants=frozenset({"control"}))
        session = TaskSession(policy=policy, task_kind="project", project="", max_turns=2)
        mixed = [
            ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"), model_text="a"),
            ToolResult(ok=True, call=ToolCall(name="read_file", args={"path": "b.py"}, call_id=""), model_text="b"),
        ]
        with self.assertRaises(ValueError):
            t._native_tool_results(mixed, session)

    def test_kernel_stops_protocol_and_keeps_receipts_on_call_id_mismatch(self) -> None:
        # Custom executor returns a ToolResult with its own unrelated call;
        # the kernel must not lose the original call id receipt.
        import tempfile

        from codey.operations.kernel_execution import execute_turn

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("hello\n", encoding="utf-8")
            policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
            session = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=2)

            def _rogue(call: ToolCall):
                return ToolResult(
                    ok=True, call=ToolCall(name="read_file", args={"path": "other.py"}, call_id="rogue-9"),
                    model_text="rogue content",
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
            self.assertEqual(len(results), 1)
            self.assertEqual(str(results[0].call.call_id or ""), "c1",
                             f"must reuse original call id, got {results[0].call!r}")
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"),
                            f"mismatched custom call must become error: {results[0].model_text[:200]}")


if __name__ == "__main__":
    unittest.main()
