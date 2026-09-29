"""Explicit executor must not bypass path policy when delegate is unavailable.

Repro: ``_build_delegate()`` returns ``None`` on construction failure, and
``_explicit_policy_denial()`` returns ``None`` (allow) for
``delegate=None``. An explicit executor therefore runs ``../outside`` paths
without any project guard.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class ExplicitExecutorDelegateUnavailableDeniesPathTraversalTests(unittest.TestCase):
    def test_delegate_unavailable_with_project_path_denies_explicit_executor(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(
                policy=_policy(), task_kind="project", project=str(project), max_turns=4
            )
            call = ToolCall(
                name="edit", args={"path": "../outside", "content": "x\n"}, call_id="c1"
            )
            called: list[int] = []

            def fake_read(_c: ToolCall):
                called.append(1)
                from codey.runtime.core.models import ToolResult
                return ToolResult(call=_c, model_text="ok")

            with mock.patch.object(
                ke, "_build_delegate", return_value=None
            ):
                results = ke.execute_turn(
                    session, [call],
                    executors={"edit": fake_read},
                    run_id="r-delegate-none", effect_scope="task", turn=1,
                    project_path=project,
                )
            self.assertEqual(called, [], f"executor must not run when delegate unavailable, got {called!r}")
            self.assertTrue(
                str(results[0].model_text or "").startswith("ERROR:"),
                f"delegate outage with project path must fail closed, got {results[0].model_text!r}",
            )


if __name__ == "__main__":
    unittest.main()
