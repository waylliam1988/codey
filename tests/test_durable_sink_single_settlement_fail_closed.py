"""Durable tool-effect ledger settles once and fails closed on rewrite.

Restores the durable-sink fail-closed coverage: the ledger itself rejects a
second settlement with a different status ("effect already settled"), so
callers must settle exactly once. Read-only policy denial is covered
separately and is not a substitute.

Lock: real RuntimeMutationLine + KernelEffectSink settle ok once; a second
settle with the opposite status raises RuntimeEffectError; identical retry
is idempotent.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class DurableSinkSingleSettlementFailClosedTests(unittest.TestCase):
    def _sink(self, tmp: Path, session_id: str, run_id: str):
        from codey.operations.task_effects import KernelEffectSink
        from codey.runtime.core.models import ToolCall
        from codey.runtime.log.session_log import RuntimeSessionLog
        from codey.runtime.write.mutation_line import RuntimeMutationLine

        log = RuntimeSessionLog(tmp / "state")
        line = RuntimeMutationLine(log)
        line.accept_operation(
            session_id=session_id, run_id=run_id, project=str(tmp),
            provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
        )
        line.mark_writer_running(session_id, run_id, provider_id="local")
        sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")
        call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        sink.begin_turn([(f"{run_id}:task#1#0", call, 0)], turn=1)
        return sink, log

    def test_second_settlement_with_different_status_raises(self) -> None:
        from codey.runtime.effects.effect_records import RuntimeEffectError

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sink, _log = self._sink(tmp, "sess-durable-1", "run-durable-1")
            identity = "run-durable-1:task#1#0"
            # First settlement ok=True commits.
            sink.settle(identity, True)
            # Identical retry is idempotent (no raise).
            sink.settle(identity, True)
            # Opposite status must fail closed.
            with self.assertRaises(RuntimeEffectError) as cm:
                sink.settle(identity, False)
            self.assertIn("already settled", str(cm.exception))

    def test_execute_turn_with_durable_sink_settles_once(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_effects import KernelEffectSink
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        from codey.runtime.log.session_log import RuntimeSessionLog
        from codey.runtime.write.mutation_line import RuntimeMutationLine

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "a.py").write_text("hello\n", encoding="utf-8")
            session_id, run_id = "sess-durable-2", "run-durable-2"
            log = RuntimeSessionLog(tmp / "state")
            line = RuntimeMutationLine(log)
            line.accept_operation(
                session_id=session_id, run_id=run_id, project=str(tmp),
                provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
            )
            line.mark_writer_running(session_id, run_id, provider_id="local")
            sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.read", "control"})),
                task_kind="project", project=str(tmp), max_turns=2,
            )
            results = execute_turn(
                session,
                [ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")],
                executors={"read_file": lambda c: ToolResult(call=c, model_text="hello")},
                run_id=run_id, effect_scope="task", turn=1,
                project_path=None, intent_sink=sink,
            )
            self.assertEqual(len(results), 1)
            identity = turn_effect_id(f"{run_id}:task", 1, 0)
            rows = RuntimeEffectStore(log).load_effects(session_id, run_id)
            settled = [r for r in rows if r.intent.effect_id == identity and r.settlement is not None]
            self.assertEqual(len(settled), 1)


if __name__ == "__main__":
    unittest.main()
