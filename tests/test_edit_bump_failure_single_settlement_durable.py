"""Edit bump failure settles once under a real durable sink.

Repro (P1): execute_turn() settled edit ok=True, then on workspace bump
failure settled the same effect ok=False. The durable ledger rejects a
second settlement for one effect ("effect already settled"), so the real
KernelEffectSink + RuntimeMutationLine path raised instead of returning a
controlled failure.

Lock: with a real durable sink, a failing bump_store must NOT raise;
execute_turn() returns one explicit ERROR result (single settlement),
marks workspace_unconfirmed, and blocks the same-batch run.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class EditBumpFailureSingleSettlementDurableTests(unittest.TestCase):
    def test_bump_failure_with_durable_sink_does_not_raise_double_settle(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_effects import KernelEffectSink
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.log.session_log import RuntimeSessionLog
        from codey.runtime.write.mutation_line import RuntimeMutationLine

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as stated:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            state_home = Path(stated)
            session_id, run_id = "sess-bump-durable-1", "run-bump-durable-1"
            log = RuntimeSessionLog(state_home)
            line = RuntimeMutationLine(log)
            line.accept_operation(
                session_id=session_id, run_id=run_id, project=str(project),
                provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
            )
            line.mark_writer_running(session_id, run_id, provider_id="local")
            sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")

            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)

            class _BoomStore:
                def bump_state(self, proj, *, ignored_paths=()):
                    raise OSError("disk gone")

            run_calls: list[str] = []

            def fake_edit(call: ToolCall):
                (project / "b.py").write_text("y=2\n", encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            def fake_run(call: ToolCall):
                run_calls.append(str(call.args.get("command") or "run"))
                return ToolResult(call=call, model_text="ok", audit={"exit_code": 0})

            # Must not raise RuntimeEffectError("effect already settled").
            results = ke.execute_turn(
                session,
                [
                    ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"}),
                    ToolCall(name="run", args={"command": "python -m unittest discover", "path": "."}),
                ],
                executors={"edit": fake_edit, "run": fake_run},
                run_id=run_id,
                effect_scope="task",
                turn=1,
                project_path=None,  # None keeps the injected executors observable
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=_BoomStore(),
                intent_sink=sink,
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"), results[0].model_text[:300])
            self.assertIn("workspace identity unconfirmed", str(results[0].model_text))
            self.assertTrue(str(results[1].model_text).startswith("ERROR:"), results[1].model_text[:300])
            self.assertEqual(run_calls, [], f"same-batch run must be blocked: {run_calls}")
            self.assertEqual(session.verifications, [])

    def test_bump_failure_single_settlement_row_in_ledger(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_effects import KernelEffectSink
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        from codey.runtime.log.session_log import RuntimeSessionLog
        from codey.runtime.write.mutation_line import RuntimeMutationLine

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as stated:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session_id, run_id = "sess-bump-durable-2", "run-bump-durable-2"
            log = RuntimeSessionLog(Path(stated))
            line = RuntimeMutationLine(log)
            line.accept_operation(
                session_id=session_id, run_id=run_id, project=str(project),
                provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
            )
            line.mark_writer_running(session_id, run_id, provider_id="local")
            sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)

            class _BoomStore:
                def bump_state(self, proj, *, ignored_paths=()):
                    raise OSError("disk gone")

            def fake_edit(call: ToolCall):
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id=run_id, effect_scope="task", turn=1,
                project_path=None,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=_BoomStore(),
                intent_sink=sink,
            )
            self.assertEqual(len(results), 1)
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"))
            identity = turn_effect_id(f"{run_id}:task", 1, 0)
            rows = RuntimeEffectStore(log).load_effects(session_id, run_id)
            settled = [r for r in rows if r.intent.effect_id == identity and r.settlement is not None]
            self.assertEqual(len(settled), 1, f"exactly one settlement expected: {rows!r:.500}")
            self.assertEqual(settled[0].settlement.status, "error")


if __name__ == "__main__":
    unittest.main()
