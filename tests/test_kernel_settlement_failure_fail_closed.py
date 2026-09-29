"""Durable settlement failure must fail closed, never silently drop a receipt.

Locks P1 ``_settle_slot``: ``session.executed[identity] = record`` is the
durable receipt. Silently suppressing its failure lets an already-executed
(possibly unsafe) tool look never-executed on the next recovery, causing a
second execution.
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "project.read", "control"}))


class KernelSettlementFailureFailClosedTests(unittest.TestCase):
    def test_executed_write_failure_raises_settlement_error(self) -> None:
        from codey.operations.kernel_errors import EffectSettlementFailed
        from codey.operations.kernel_execution import _settle_slot
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        result = ToolResult(call=call, model_text="content")

        class BoomDict(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("durable receipt boom")

        session.executed = BoomDict()  # type: ignore[assignment]
        with self.assertRaises(EffectSettlementFailed) as ctx:
            _settle_slot(session, "slot-1", call, result, ok=True)
        self.assertIn("settlement", str(ctx.exception).lower())

    def test_settlement_failure_blocks_batch_as_provider_failure(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")

        def fake_read(_c: ToolCall):
            return ToolResult(call=_c, model_text="content")

        class BoomDict(dict):
            def __setitem__(self, key, value):
                raise RuntimeError("durable receipt boom")

        session.executed = BoomDict()  # type: ignore[assignment]
        # Settlement outage must not return a bare success; execute_turn must
        # surface it (raise for the loop to map to provider_failure, or
        # return explicit ERROR). Executor already ran, so the batch must not
        # continue as if settled.
        try:
            results = execute_turn(
                session, [call],
                executors={"read_file": fake_read},
                run_id="r-settle-fail-1", effect_scope="task", turn=1,
            )
        except Exception as exc:
            from codey.operations.kernel_errors import EffectSettlementFailed

            self.assertIsInstance(exc, EffectSettlementFailed)
            return
        text = str(results[0].model_text or "")
        self.assertTrue(text.startswith("ERROR:"), f"settlement failure must be ERROR, got {text!r}")

    def test_memory_cache_failure_stays_tolerant(self) -> None:
        """Process-local _memory_results is a cache: its failure never blocks."""
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")

        def fake_read(_c: ToolCall):
            return ToolResult(call=_c, model_text="content")

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
