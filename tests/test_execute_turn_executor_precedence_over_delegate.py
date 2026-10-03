"""Injected executors take precedence over the ExecutionDelegate.

Repro (P2): with a real project_path directory, _build_delegate() creates an
ExecutionDelegate that handles project tools, so executors={"edit": fake}
were silently ignored. Tests asserting on fake counters (run_calls == [])
proved nothing about the block.

Lock: when executors contains the tool name, the injected fn runs even with
a real project_path; the delegate is only the fallback.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "project.verify", "control"}))


class ExecutorPrecedenceOverDelegateTests(unittest.TestCase):
    def test_injected_edit_fn_runs_with_real_project_path(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            calls: list[str] = []

            def fake_edit(call: ToolCall):
                calls.append(str(call.args.get("path") or ""))
                return ToolResult(ok=True, call=call, model_text="fake-edited", audit={"changed": False})

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id="r-prec-1", effect_scope="task", turn=1,
                project_path=project,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=None,
            )
            self.assertEqual(calls, ["b.py"], "injected executor must run, not the delegate")
            self.assertEqual(str(results[0].model_text), "fake-edited")

    def test_injected_run_fn_runs_with_real_project_path(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            calls: list[str] = []

            def fake_run(call: ToolCall):
                calls.append(str(call.args.get("command") or ""))
                return ToolResult(ok=True, call=call, model_text="fake-run", audit={"exit_code": 0})

            ke.execute_turn(
                session,
                [ToolCall(name="run", args={"command": "python -m unittest discover", "path": "."})],
                executors={"run": fake_run},
                run_id="r-prec-2", effect_scope="task", turn=1,
                project_path=project,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=None,
            )
            self.assertEqual(calls, ["python -m unittest discover"])
            self.assertTrue(session.verifications, "fake run with exit 0 must record verification")


if __name__ == "__main__":
    unittest.main()
