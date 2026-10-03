"""Event projection failures terminate the kernel with one recovery outcome."""
from __future__ import annotations

import unittest
from unittest import mock

from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps


class KernelEventFailureReturnsRecoveryFailureTests(unittest.TestCase):
    def test_projection_failure_returns_recovery_failure_and_closes_tool_event(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations.task_loop import run_task_kernel
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolResult

        class Provider:
            def send(self, _prompt, timeout=None):
                return '{"tool":"edit","args":{"path":"a.py","content":"x=2"}}'

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            from codey.workspace.revision import WorkspaceRevisionStore

            calls = []

            def edit(call):
                calls.append(call)
                (project / "a.py").write_text("x=2\n", encoding="utf-8")
                return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
                task_kind="project",
                project=str(project),
                max_turns=2,
            )
            events = []
            with mock.patch(
                "codey.operations.kernel_provenance.attach_proof_to_event",
                side_effect=RuntimeError("event proof unavailable"),
            ):
                result = run_task_kernel(
                    session,
                    request=KernelRunRequest(
                        transport=KernelTransportDeps(
                            provider=Provider(),
                            run_id="r-event-failure",
                            effect_scope="task",
                        ),
                        execution=KernelExecutionDeps(
                            executors={"edit": edit},
                            project_path=project,
                            workspace_revision_store=WorkspaceRevisionStore(Path(home)),
                        ),
                        observation=KernelObservationDeps(
                            on_event=events.append,
                        ),
                    ),
                )

        self.assertEqual(result.stop_reason, "recovery_failure")
        self.assertEqual(len(calls), 1)
        self.assertEqual([event.kind for event in events], ["turn", "tool_start", "tool"])

    def test_entry_converts_projection_recovery_failure_to_task_done(self) -> None:
        import tempfile
        from types import SimpleNamespace

        from codey.app.context import AppContext
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.task_entry import run_entry_kernel
        from codey.task.model import TaskSubmission

        request = TaskSubmission("s", ".", "inspect", 1, False, "local", intent="project", run_id="r")
        frame = SimpleNamespace(
            request=request,
            task_kind="project",
            run_id="r",
            provider=SimpleNamespace(send=lambda *_args, **_kwargs: ""),
            provider_id="local",
            project_text=".",
            handoff="",
            recovered_tool_outcomes=(),
            recovered_tool_result_batch_id="",
        )
        hooks = SimpleNamespace(on_event=lambda _event: None, on_shell_request=None)
        deps = SimpleNamespace(knowledge_store=None, search_factory=None, runtime_mutations=None, state=None)
        with (
            tempfile.TemporaryDirectory() as state_home,
            mock.patch("codey.operations.task_loop.run_task_kernel",
                       side_effect=RecoveryFailed("event proof unavailable")) as kernel,
        ):
            deps.state = AppContext(state_home)
            result = run_entry_kernel(
                frame,
                SimpleNamespace(evidence=None, analysis_run_payloads=[]),
                hooks,
                deps,
            )

        self.assertEqual(result.event["type"], "task_done")
        self.assertEqual(result.event["stop_reason"], "recovery_failure")
        kernel.assert_called_once()
        self.assertIn("event proof unavailable", result.event["summary"])


if __name__ == "__main__":
    unittest.main()
