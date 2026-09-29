"""Fresh guard denials must settle the receipt; replays must not rewrite it.

Locks settlement reconciliation:
- policy-denied fresh slots settle as errors (intent closes, receipt exists)
- already-durable replays never rewrite ``session.executed``
"""
from __future__ import annotations

import unittest


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.read", "control"}))


class GuardedFreshDenialSettlesReceiptTests(unittest.TestCase):
    def test_policy_denied_fresh_slot_settles(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="shell", args={"command": "rm -rf /", "path": "."}, call_id="c1")

        def fail_shell(_c: ToolCall):
            raise AssertionError("policy-denied shell must never reach the executor")

        results = execute_turn(
            session, [call],
            executors={"shell": fail_shell},
            run_id="r-guard-settle-1", effect_scope="task", turn=1,
        )
        self.assertTrue(str(results[0].model_text or "").startswith("ERROR:"))
        identity = turn_effect_id("r-guard-settle-1:task", 1, 0)
        self.assertIn(identity, session.executed)

    def test_replay_never_rewrites_durable_receipt(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall
        from codey.runtime.effects.effect_records import compute_args_digest

        session = TaskSession(policy=_policy(), task_kind="project", project="p", max_turns=4)
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        identity = turn_effect_id("r-guard-settle-2:task", 1, 0)
        session.executed[identity] = {
            "name": "read_file", "ok": True, "call_id": "c1",
            "excerpt": "original", "args_digest": compute_args_digest(call.args),
        }
        before = dict(session.executed[identity])

        def evil_read(_c: ToolCall):
            raise AssertionError("replay must never reach the executor")

        results = execute_turn(
            session, [call],
            executors={"read_file": evil_read},
            run_id="r-guard-settle-2", effect_scope="task", turn=1,
        )
        self.assertEqual(str(results[0].model_text or ""), "original")
        self.assertEqual(session.executed[identity], before)


if __name__ == "__main__":
    unittest.main()
