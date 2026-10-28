"""A failed authoritative receipt cannot publish a successful live projection."""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "project.read", "control"}))


class KernelSettlementFailureFailClosedTests(unittest.TestCase):
    def test_sink_failure_does_not_publish_executed_projection(self) -> None:
        from unittest.mock import Mock

        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        executor = Mock(side_effect=lambda item: ToolResult(ok=True, call=item, model_text="content"))
        sink = Mock(has_unsettled=Mock(return_value=False), settle=Mock(side_effect=OSError("receipt write failed")))
        with self.assertRaisesRegex(OSError, "receipt write failed"):
            execute_turn(session, [call], executors={"read_file": executor}, run_id="r", turn=1, intent_sink=sink)
        executor.assert_called_once_with(call)
        sink.settle.assert_called_once()
        self.assertEqual(session.executed, {})
        self.assertEqual(session._memory_results, {})

    def test_settlement_failure_raises_settlement_error(self) -> None:
        from codey.operations.kernel_errors import EffectSettlementFailed
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")

        def fake_read(_c: ToolCall):
            return ToolResult(ok=True, call=_c, model_text="content")

        class BoomDict(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("durable receipt boom")

        session.executed = BoomDict()  # type: ignore[assignment]
        # A projection failure must be explicit and cannot return success.
        with self.assertRaises(EffectSettlementFailed):
            execute_turn(
                session, [call],
                executors={"read_file": fake_read},
                run_id="r-settle-fail-1", effect_scope="task", turn=1,
            )

    def test_memory_cache_failure_stays_tolerant(self) -> None:
        """Process-local _memory_results is a cache: its failure never blocks."""
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")

        def fake_read(_c: ToolCall):
            return ToolResult(ok=True, call=_c, model_text="content")

        class BoomMemory(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("cache boom")

        session._memory_results = BoomMemory()  # type: ignore[assignment]
        results = execute_turn(
            session, [call],
            executors={"read_file": fake_read},
            run_id="r-settle-cache-1", effect_scope="task", turn=1,
        )
        self.assertFalse(str(results[0].model_text or "").startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
