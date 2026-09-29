"""Recovery control must use typed disposition, never model-text prefix.

Locks P2 text-prefix fragility:
- a legitimate safe excerpt starting with ``ERROR: recovery failed`` is
  still a successful replay (RECOVERED), not a FAILED batch
- batch pre-check reads disposition, not ``str.startswith`` on display text
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.read", "control"}))


class RecoverySlotDispositionTypedTests(unittest.TestCase):
    def test_safe_excerpt_with_error_prefix_is_not_failed(self) -> None:
        from codey.operations.kernel_recovery import _check_batch_recovery
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        identity = turn_effect_id("r-disposition-1:task", 1, 0)
        # Safe settled record whose excerpt happens to start with the old
        # error prefix: must not abort the batch as FAILED.
        from codey.runtime.effects.effect_records import compute_args_digest

        session.executed[identity] = {
            "name": "read_file",
            "ok": True,
            "call_id": "c1",
            "excerpt": "ERROR: recovery failed: this is real file content",
            "args_digest": compute_args_digest(call.args),
        }
        check = _check_batch_recovery(
            session, [call], {}, "r-disposition-1:task", 1, 0
        )
        self.assertEqual(check.kind, "NO_MATCH")

    def test_typed_slot_result_carries_disposition(self) -> None:
        from codey.operations.kernel_recovery import replay_slot_typed
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        identity = turn_effect_id("r-disposition-2:task", 1, 0)
        from codey.runtime.effects.effect_records import compute_args_digest

        session.executed[identity] = {
            "name": "read_file",
            "ok": True,
            "call_id": "c1",
            "excerpt": "content",
            "args_digest": compute_args_digest(call.args),
        }
        slot = replay_slot_typed(session, identity, call, "read_file", 1)
        self.assertEqual(slot.disposition, "RECOVERED")
        self.assertIsNotNone(slot.result)


if __name__ == "__main__":
    unittest.main()
