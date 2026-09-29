"""Recovery errors must abort the batch without running any executor.

Locks:
- batch pre-check exceptions are recovery errors, never silent None.
- delivered rebuild failure aborts the batch instead of returning stored.
- persisted replay keeps side-channel or fails closed; suppress must not
  hide the attach failure.
"""
from __future__ import annotations

import unittest
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class KernelRecoveryErrorAbortsBatchWithoutExecutorTests(unittest.TestCase):
    def test_batch_check_exception_never_runs_executor(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        called: list[int] = []

        def fake_edit(_c: ToolCall):
            called.append(1)
            from codey.runtime.core.models import ToolResult
            return ToolResult(call=_c, model_text="edited")

        # Tri-state pre-check outage must abort without invoking the executor.
        # Both the recovery module and the execution orchestrator's bound
        # references are patched so the pre-check sees the outage.
        with mock.patch(
            "codey.operations.kernel_recovery.delivered_slot_typed",
            side_effect=RuntimeError("delivery boom"),
        ), mock.patch(
            "codey.operations.kernel_execution.delivered_slot_typed",
            side_effect=RuntimeError("delivery boom"),
        ):
            results = ke.execute_turn(
                session, [call],
                executors={"edit": fake_edit},
                run_id="r-batch-err-1", effect_scope="task", turn=1,
            )
        self.assertEqual(called, [], f"recovery check failure must not run executor, got {called!r}")
        self.assertTrue(
            str(results[0].model_text or "").startswith("ERROR:"),
            f"recovery check failure must be ERROR, got {results[0].model_text!r}",
        )

    def test_delivered_rebuild_failure_does_not_return_bare_stored(self) -> None:
        from codey.operations.kernel_recovery import _delivered_slot_result
        from codey.operations.task_session import turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        stored = ToolResult(call=call, model_text="stored-bare")
        identity = turn_effect_id("r-batch-err-2:task", 1, 0)
        delivered = {identity: stored}
        with mock.patch(
            "codey.operations.kernel_result.build_recovered_tool_result",
            side_effect=RuntimeError("rebuild boom"),
        ):
            result = _delivered_slot_result(delivered, identity, call)
        self.assertIsNotNone(result)
        assert result is not None
        text = str(result.model_text or "")
        # Must be an explicit recovery error, never the bare stored object.
        self.assertTrue(
            text.startswith("ERROR:"),
            f"delivered rebuild failure must be ERROR, got {text!r}",
        )
        self.assertIsNot(result, stored)


if __name__ == "__main__":
    unittest.main()
