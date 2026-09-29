"""Batch recovery pre-check is tri-state: NO_MATCH / MISMATCH / FAILED.

Locks P2: ``_batch_recovery_mismatch`` returning only ``bool`` collapses a
real recovery construction failure into a generic mismatch ERROR. The
pre-check must distinguish so callers emit mismatch vs failed results,
never invoke the executor in either case, and preserve receipts.
"""
from __future__ import annotations

import unittest
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class KernelRecoveryTriStateTests(unittest.TestCase):
    def test_no_match_returns_no_match(self) -> None:
        from codey.operations.kernel_recovery import _check_batch_recovery
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        result = _check_batch_recovery(session, [call], {}, "r-tri-1", 1, 0)
        self.assertEqual(result.kind, "NO_MATCH")

    def test_call_mismatch_is_mismatch_not_failed(self) -> None:
        from codey.operations.kernel_recovery import _check_batch_recovery
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        identity = turn_effect_id("r-tri-2:task", 1, 0)
        session.executed[identity] = {
            "name": "read_file", "ok": True, "call_id": "c1",
            "excerpt": "old", "args_digest": "digest-other",
        }
        other = ToolCall(name="edit", args={"path": "other.py", "content": "y\n"}, call_id="c1")
        result = _check_batch_recovery(session, [other], {}, "r-tri-2:task", 1, 0)
        self.assertEqual(result.kind, "MISMATCH")

    def test_recovery_construction_failure_is_failed_not_mismatch(self) -> None:
        from codey.operations.kernel_recovery import _check_batch_recovery
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        with mock.patch(
            "codey.operations.kernel_recovery._delivered_slot_result",
            side_effect=RuntimeError("delivery boom"),
        ):
            result = _check_batch_recovery(session, [call], {"k": mock.MagicMock()}, "r-tri-3", 1, 0)
        # Even when the delivered map lookup itself explodes, the check must
        # report FAILED (recovery error), never MISMATCH and never NO_MATCH.
        self.assertEqual(result.kind, "FAILED")
        self.assertTrue(str(result.message or "").strip() != "")

    def test_failed_precheck_emits_failed_results_without_executor(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x\n"}, call_id="c1")
        called: list[int] = []

        def fake_edit(_c: ToolCall):
            called.append(1)
            return ToolResult(call=_c, model_text="edited")

        with mock.patch(
            "codey.operations.kernel_recovery._delivered_slot_result",
            side_effect=RuntimeError("delivery boom"),
        ), mock.patch(
            "codey.operations.kernel_execution._delivered_slot_result",
            side_effect=RuntimeError("delivery boom"),
        ):
            # Pre-check outage with a non-empty delivered map must fail the
            # batch without invoking the executor.
            from codey.operations.task_session import turn_effect_id

            identity = turn_effect_id("r-tri-4:task", 1, 0)
            delivered = {identity: ToolResult(call=call, model_text="stored")}
            results = ke.execute_turn(
                session, [call],
                executors={"edit": fake_edit},
                run_id="r-tri-4", effect_scope="task", turn=1,
                delivered=delivered,
            )
        self.assertEqual(called, [])
        text = str(results[0].model_text or "")
        self.assertTrue(
            text.startswith("ERROR: recovery failed"),
            f"construction failure must be recovery failed, not mismatch, got {text!r}",
        )
        self.assertNotIn("recovery mismatch", text.lower())


if __name__ == "__main__":
    unittest.main()
