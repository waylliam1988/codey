"""Event projection failures terminate the kernel with one recovery outcome."""
from __future__ import annotations

import unittest
from typing import Never
from unittest import mock

from codey.operations.provider_session import adapt_provider
from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps
from tests.support.kernel_fixture import admit, run_task_kernel


class KernelEventFailureReturnsRecoveryFailureTests(unittest.TestCase):
    def test_projection_failure_returns_recovery_failure_and_closes_tool_event(self) -> None:
        import tempfile
        from pathlib import Path

        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.events import RunEvent

        class Provider:
            def send(self, _prompt: str, timeout: float | None = None) -> str:
                return '{"tool":"edit","args":{"path":"a.py","content":"x=2"}}'

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            from codey.workspace.revision import WorkspaceRevisionStore

            calls: list[ToolCall] = []

            def edit(call: ToolCall) -> ToolResult:
                calls.append(call)
                (project / "a.py").write_text("x=2\n", encoding="utf-8")
                return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

            session = TaskSession(
                policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
                task_kind="project",
                project=str(project),
                max_turns=2,
            )
            events: list[RunEvent] = []
            with mock.patch(
                "codey.operations.kernel_provenance.attach_proof_to_event",
                side_effect=RuntimeError("event proof unavailable"),
            ):
                result = run_task_kernel(
                    session,
                    request=KernelRunRequest(
                        transport=KernelTransportDeps(
                            provider=adapt_provider(Provider()),
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
        kinds: tuple[str, ...] = tuple(event.kind for event in events)
        self.assertEqual(kinds, ("turn", "tool_start", "tool"))

    def test_entry_converts_projection_recovery_failure_to_task_done(self) -> None:
        import tempfile
        from contextlib import closing
        from dataclasses import replace

        from codey.app.headless_runner import HeadlessAppContext
        from codey.operations.context import RunHooks, RunWork
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.task_entry import run_entry_kernel
        from codey.operations.task_run import TaskRunDeps
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.task.model import TaskSubmission
        from tests.support.task_entry_helpers import entry_frame

        def unexpected(*_args: object, **_kwargs: object) -> Never:
            raise AssertionError("Recovery must not execute a provider, tool or review")

        class Provider:
            def send(self, prompt: str) -> str:
                return unexpected(prompt)

        with (
            tempfile.TemporaryDirectory() as state_home,
            tempfile.TemporaryDirectory() as project,
            closing(HeadlessAppContext(state_home, port=0, emit_jsonl=lambda _row: None)) as state,
            mock.patch("codey.operations.task_loop.run_task_kernel",
                       side_effect=RecoveryFailed("event proof unavailable")) as kernel,
        ):
            request = TaskSubmission("s", project, "inspect", 1, False, "local", intent="project", run_id="r")
            frame = replace(entry_frame(request), provider=adapt_provider(Provider()),
                entry_policy=TaskPolicy(frozenset({"control", "project.read"})))
            hooks = RunHooks(on_event=lambda _event: None, on_shell_request=unexpected, update_checkpoint=unexpected,
                record_provider_failure=unexpected, append_ledger=unexpected, provider_failover_order=lambda: (), supervisor=None)
            mutations = admit(state.durable, run_id="r", session_id="s", task="inspect", provider_id="local", max_turns=1)
            deps = TaskRunDeps(state=state, agent_run=unexpected, collect_changes=unexpected, run_review=unexpected,
                capture_provider_failure=unexpected, workspace_revisions=state.workspace_revisions,
                runtime_mutations=mutations, managed_outputs=state.managed_outputs)
            result = run_entry_kernel(frame, RunWork(recent_events=[], evidence=ExecutionEvidence()), hooks, deps)

        self.assertEqual(result.event["type"], "task_done")
        self.assertEqual(result.event["stop_reason"], "recovery_failure")
        kernel.assert_called_once()
        summary = result.event["summary"]
        assert isinstance(summary, str)
        self.assertIn("event proof unavailable", summary)


if __name__ == "__main__":
    unittest.main()
