"""Recovered construction and trusted attach must fail closed.

Locks:
- ``build_recovered_tool_result`` failure is never a bare success.
- ``_with_trusted_workspace_state`` side-channel failure is never a silent
  trusted-looking original; the edit must become unconfirmed, not trusted.
"""
from __future__ import annotations

import unittest
from unittest import mock


class KernelRecoveredResultAndTrustedAttachFailClosedTests(unittest.TestCase):
    def test_build_recovered_failure_is_not_bare_success(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.runtime.core.models import ToolCall

        call = ToolCall(name="edit", args={"path": "a.py"}, call_id="c1")
        with mock.patch(
            "codey.operations.kernel_result.ToolResult", side_effect=RuntimeError("ctor boom")
        ):
            try:
                result = ke.build_recovered_tool_result(call, model_text="edited")
            except Exception as exc:
                self.assertTrue(str(exc) or True)
                return
            # If it returns, it must be an explicit error, never bare success.
            text = str(getattr(result, "model_text", "") or "")
            self.assertTrue(
                text.startswith("ERROR:"),
                f"recovered ctor failure must be ERROR, got {text!r}",
            )

    def test_trusted_side_channel_failure_is_unconfirmed(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceRevisionStore

        def _policy():
            from codey.policies.task_policy import TaskPolicy
            return TaskPolicy(grants=frozenset({"project.write", "control"}))

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            session = TaskSession(
                policy=_policy(), task_kind="project", project=str(project), max_turns=4
            )
            call = ToolCall(name="edit", args={"path": "a.py", "content": "x=2\n"}, call_id="c1")

            def fake_edit(_c: ToolCall):
                (project / "a.py").write_text("x=2\n", encoding="utf-8")
                return ToolResult(call=_c, model_text="edited", audit={"changed": True})

            with mock.patch(
                "codey.operations.kernel_provenance._with_trusted_workspace_state",
                side_effect=RuntimeError("attach boom"),
            ), mock.patch.object(
                ke, "_with_trusted_workspace_state",
                side_effect=RuntimeError("attach boom"),
            ):
                results = ke.execute_turn(
                    session, [call],
                    executors={"edit": fake_edit},
                    run_id="r-attach-fail-1", effect_scope="task", turn=1,
                    project_path=project,
                    workspace_revision_store=store,
                )
            # Attach outage must settle as unconfirmed ERROR in-band (edit
            # happened, identity unconfirmed) and carry no trusted identity,
            # never raise past execute_turn and never look like success.
            text = str(results[0].model_text or "")
            rev, fp = ke._trusted_workspace_from_result(results[0])
            self.assertTrue(
                text.startswith("ERROR:"),
                f"attach failure must be explicit ERROR, got {text!r}",
            )
            self.assertEqual((rev, fp), (0, ""))


if __name__ == "__main__":
    unittest.main()
