from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from codey.app import server
from codey.operations.context import RunWork
from codey.operations.project_completion_flow import (
    ProjectCompletionDeps,
    handle_project_tool_event,
)
from codey.runtime.events import RunEvent
from codey.runtime.execution_evidence import ExecutionEvidence
from codey.runtime.models import ToolCall
from codey.toolchain.runtime import ToolOutcome


def _outcome(
    *,
    ok: bool = True,
    exit_code: int = 0,
    managed_output: dict | None = None,
) -> ToolOutcome:
    audit: dict[str, object] = {
        "command_started_at": "2026-08-22T08:00:00.000Z",
        "command_finished_at": "2026-08-22T08:00:01.000Z",
        "command_duration_ms": 1000,
    }
    if managed_output:
        audit["managed_output"] = managed_output
    return ToolOutcome(
        f"exit {exit_code}: pytest -q\n1 passed",
        ok,
        audit=audit,
        exit_code=exit_code,
    )


def _run_event(outcome: ToolOutcome, *, name: str = "run", index: int = 2) -> RunEvent:
    return RunEvent.tool_finished(
        turn=1,
        call=ToolCall(name=name, args={"command": "pytest -q", "path": "."}),
        outcome=outcome,
        index=index,
    )


class AnalysisRunIntegrationTests(unittest.TestCase):
    def test_successful_run_projects_analysis_artifact_and_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            recorder = state.run_traces.open(
                run_id="run-analysis",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []

            self._handle_project_event(
                state,
                event=_run_event(
                    _outcome(
                        managed_output={
                            "handle": "out_0001_abc123def456",
                            "original_bytes": 90000,
                            "stored_bytes": 40000,
                            "sha256": "a" * 64,
                            "stored_truncated": True,
                        }
                    )
                ),
                project=str(td),
                work=work,
                run_id="run-analysis",
                update_checkpoint=self._checkpoint_recorder(checkpoints),
            )
            recorder.finish(status="done")

            payload = recorder.manifest.to_payload()
            self.assertEqual(len(payload["analysis_runs"]), 1)
            entry = payload["analysis_runs"][0]
            self.assertEqual(entry["tool_id"], "1:2")
            self.assertEqual(entry["tool_name"], "run")
            self.assertEqual(entry["exit_code"], 0)
            self.assertTrue(entry["ok"])
            self.assertEqual(entry["capture_quality"], "output_captured")
            self.assertEqual(entry["reproduction_status"], "output_captured")

            self.assertEqual(len(payload["artifact_refs"]), 1)
            artifact = payload["artifact_refs"][0]
            self.assertEqual(artifact["mime"], "text/plain")
            self.assertEqual(artifact["origin_run_id"], "run-analysis")
            self.assertEqual(artifact["produced_by"], entry["analysis_run_id"])

            self.assertEqual(len(payload["reproducibility_capsules"]), 1)
            capsule = payload["reproducibility_capsules"][0]
            self.assertEqual(capsule["reproduction_status"], "output_captured")
            self.assertEqual(capsule["analysis_run_refs"], [entry["analysis_run_id"]])

            # Checkpoint and project facts still behave exactly as before.
            self.assertEqual(checkpoints, [("pytest -q", ".", True)])
            facts = state.project_facts.load(str(td))
            commands = [item.command for item in facts.commands]
            self.assertIn("pytest -q", commands)

    def test_failed_run_records_failure_and_skips_project_facts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            recorder = state.run_traces.open(
                run_id="run-failed",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []

            self._handle_project_event(
                state,
                event=_run_event(_outcome(ok=False, exit_code=2)),
                project=str(td),
                work=work,
                run_id="run-failed",
                update_checkpoint=self._checkpoint_recorder(checkpoints),
            )
            recorder.finish(status="done")

            payload = recorder.manifest.to_payload()
            self.assertEqual(len(payload["analysis_runs"]), 1)
            self.assertEqual(payload["analysis_runs"][0]["reproduction_status"], "failed")
            self.assertEqual(len(payload["artifact_refs"]), 0)
            self.assertEqual(payload["reproducibility_capsules"][0]["reproduction_status"], "failed")
            self.assertEqual(checkpoints, [("pytest -q", ".", False)])
            self.assertEqual(state.project_facts.load(str(td)).commands, ())

    def test_edit_event_keeps_checkpoint_branch_without_analysis_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            recorder = state.run_traces.open(
                run_id="run-edit",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []
            outcome = ToolOutcome("saved", True, changed=True)

            self._handle_project_event(
                state,
                event=RunEvent.tool_finished(
                    turn=1,
                    call=ToolCall(name="edit", args={"path": "snake.py"}),
                    outcome=outcome,
                ),
                project=str(td),
                work=work,
                run_id="run-edit",
                update_checkpoint=self._checkpoint_recorder(checkpoints),
            )

            self.assertEqual(checkpoints, [("snake.py", "", True)])
            self.assertEqual(recorder.manifest.analysis_runs, [])

    def test_non_execution_outcomes_are_not_projected(self) -> None:
        """Policy deny / bad cwd / command-not-found outcomes carry no timing.

        They must stay out of the execution audit: the roadmap records
        existing executions, not attempts.
        """

        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            recorder = state.run_traces.open(
                run_id="run-denied",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []

            denied = ToolOutcome(
                "ERROR: this command is not allowed by policy",
                False,
                audit={"error_code": "policy_denied"},
                error_code="policy_denied",
            )
            self._handle_project_event(
                state,
                event=_run_event(denied),
                project=str(td),
                work=work,
                run_id="run-denied",
                update_checkpoint=self._checkpoint_recorder(checkpoints),
            )
            recorder.finish(status="done")

            payload = recorder.manifest.to_payload()
            self.assertEqual(payload["analysis_runs"], [])
            self.assertEqual(payload["artifact_refs"], [])
            self.assertEqual(payload["reproducibility_capsules"], [])
            # The checkpoint still tracks the attempt exactly as before.
            self.assertEqual(checkpoints, [("pytest -q", ".", False)])

    def test_timed_out_execution_is_recorded_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)
            recorder = state.run_traces.open(
                run_id="run-timeout",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)

            timed_out = ToolOutcome(
                "command timed out after 30s (this is a timeout, not a test failure)",
                False,
                audit={
                    "error_code": "timeout",
                    "command_started_at": "2026-08-22T08:00:00.000Z",
                    "command_finished_at": "2026-08-22T08:00:30.000Z",
                    "command_duration_ms": 30000,
                },
                error_code="timeout",
            )
            self._handle_project_event(
                state,
                event=_run_event(timed_out),
                project=str(td),
                work=work,
                run_id="run-timeout",
                update_checkpoint=self._checkpoint_recorder([]),
            )
            recorder.finish(status="done")

            payload = recorder.manifest.to_payload()
            self.assertEqual(len(payload["analysis_runs"]), 1)
            entry = payload["analysis_runs"][0]
            self.assertEqual(entry["reproduction_status"], "failed")
            self.assertEqual(entry["exit_code"], None)
            self.assertEqual(entry["duration_ms"], 30000)

    def test_trace_failures_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.AppContext(td)

            class _ExplodingTrace:
                def record_analysis_run(self, _payload):
                    raise RuntimeError("trace disk full")

            work = RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=_ExplodingTrace())

            self._handle_project_event(
                state,
                event=_run_event(_outcome()),
                project=str(td),
                work=work,
                run_id="run-explodes",
                update_checkpoint=self._checkpoint_recorder([]),
            )

    @staticmethod
    def _checkpoint_recorder(captured: list):
        def record(kind: str, value: str):
            captured.append(value)

        def update_checkpoint(fn) -> None:
            class _Store:
                def record_run(
                    self,
                    item,
                    *,
                    command,
                    cwd,
                    ok,
                    workspace_revision,
                    workspace_fingerprint,
                ):
                    del workspace_revision, workspace_fingerprint
                    record("run", (command, cwd, ok))

                def record_edit(self, item, rel):
                    record("edit", (rel, "", True))

            fn(_Store(), object())

        return update_checkpoint

    def _handle_project_event(
        self,
        state: server.AppContext,
        **kwargs,
    ) -> None:
        deps = ProjectCompletionDeps(
            state=state,
            agent_run=mock.Mock(),
            collect_changes=mock.Mock(),
            run_review=mock.Mock(),
            capture_provider_failure=server.capture_provider_failure,
            commit_run_operation=mock.Mock(),
            project_facts=state.project_facts,
            workspace_revisions=state.workspace_revisions,
        )
        handle_project_tool_event(deps, **kwargs)


if __name__ == "__main__":
    unittest.main()
