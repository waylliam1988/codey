"""execute_turn rejects digest mismatch on the real turn slot.

Repro: the old test wrote the prior receipt at "k-mismatch", not the real
turn_effect_id for this turn, then only asserted the result was non-empty.
It never proved "mismatch is rejected".

Lock: use the real effect id for run/turn/index, store a different tool
and args digest, then assert the executor is never called (0 calls), the
batch returns an explicit mismatch error, and the original receipt is
unchanged.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class ExecuteTurnRealSlotDigestMismatchTests(unittest.TestCase):
    def test_mismatch_on_real_slot_executes_nothing_and_keeps_receipt(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "target.py").write_text("x=1\n", encoding="utf-8")
            policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
            session = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=2)
            run_id = "r-mismatch-1"
            real_identity = turn_effect_id(run_id, 1, 0)
            before = {
                "name": "read_file",
                "ok": True,
                "call_id": "call-before",
                "excerpt": "old content",
                "args_digest": "digest-read-target",
            }
            session.executed[real_identity] = dict(before)
            session._memory_results[real_identity] = ToolResult(
                call=ToolCall(name="read_file", args={"path": "target.py"}, call_id="call-before"),
                model_text="old content",
            )
            calls_made: list[ToolCall] = []

            def _grep(call: ToolCall):
                calls_made.append(call)
                return ToolResult(call=call, model_text="hits")

            results = execute_turn(
                session,
                [ToolCall(name="grep", args={"path": ".", "query": "q"}, call_id="call-new")],
                executors={"grep": _grep},
                run_id=run_id,
                turn=1,
                project_path=project,
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(calls_made, [], "mismatched slot must not execute")
            self.assertTrue(
                str(results[0].model_text).startswith("ERROR: recovery mismatch"),
                f"must return explicit mismatch: {results[0].model_text[:300]}",
            )
            self.assertEqual(session.executed[real_identity], before, "original receipt must be unchanged")
            stored = session._memory_results[real_identity]
            self.assertEqual(stored.model_text, "old content")


if __name__ == "__main__":
    unittest.main()
