"""Delegate run with bool/str exit_code never counts as pass.

Incidental deterministic bug found while fixing strict exits: the delegate
path returned the raw exit_code from tool_fns; a bool False would flow into
record_facts and be stored as int 0. The kernel now coerces non-int exits
to None and marks ok=False.

Lock: delegate returning exit_code=False records a failed observation
(passed=False, no exit_code), never a pass and never exit 0.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class DelegateRunBoolExitCodeRejectedTests(unittest.TestCase):
    def test_delegate_bool_exit_records_no_verification(self) -> None:
        from codey.operations.kernel_execution import execute_turn
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.read", "project.verify", "control"})),
                task_kind="project", project=str(project), max_turns=2,
            )

            class _FakeDelegate:
                def handles(self, name: str) -> bool:
                    return str(name or "").lower() == "run"

                def execute(self, call, *, turn=0, tool_index=0):
                    return (
                        ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": False}),
                        True,
                        False,
                    )

            import unittest.mock as mock

            with mock.patch(
                "codey.operations.kernel_execution._build_delegate", return_value=_FakeDelegate()
            ):
                results = execute_turn(
                    session,
                    [ToolCall(name="run", args={"command": "echo hi", "path": "."})],
                    executors={},
                    run_id="r-delegate-bool-1", turn=1,
                    project_path=project,
                )
            self.assertEqual(len(results), 1)
            self.assertEqual(len(session.verifications), 1)
            self.assertFalse(session.verifications[0]["passed"])
            self.assertNotIn("exit_code", session.verifications[0])


if __name__ == "__main__":
    unittest.main()
