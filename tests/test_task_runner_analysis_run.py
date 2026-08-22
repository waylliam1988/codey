from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from codey import server
from codey.agent import RunResult
from codey.events import RunEvent
from codey.execution_evidence import ExecutionEvidence
from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome
from codey.task_runner import TaskRunner, _RunWork


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


def _run_event(outcome: ToolOutcome, *, name: str = "run") -> RunEvent:
    return RunEvent.tool_finished(
        turn=1,
        call=ToolCall(name=name, args={"command": "pytest -q", "path": "."}),
        outcome=outcome,
    )


class AnalysisRunIntegrationTests(unittest.TestCase):
    def test_successful_run_projects_analysis_artifact_and_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            runner = self._runner(state)
            recorder = state.run_traces.open(
                run_id="run-analysis",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = _RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []

            runner._handle_project_tool_event(
                event=_run_event(_outcome(managed_output={
                    "handle": "out_0001_abc123def456",
                    "original_bytes": 90000,
                    "stored_bytes": 40000,
                    "sha256": "a" * 64,
                    "stored_truncated": True,
                })),
                project=str(td),
                work=work,
                run_id="run-analysis",
                update_checkpoint=self._checkpoint_recorder(checkpoints),
            )
            recorder.finish(status="done")

            payload = recorder.manifest.to_payload()
            self.assertEqual(len(payload["analysis_runs"]), 1)
            entry = payload["analysis_runs"][0]
            self.assertEqual(entry["tool_id"], "run")
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
            state = server.State(td)
            runner = self._runner(state)
            recorder = state.run_traces.open(
                run_id="run-failed",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = _RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []

            runner._handle_project_tool_event(
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
            state = server.State(td)
            runner = self._runner(state)
            recorder = state.run_traces.open(
                run_id="run-edit",
                session_id="session-analysis",
                project=None,
                mode_initial="project",
                provider_initial="deepseek",
            )
            work = _RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=recorder)
            checkpoints: list[tuple[str, str, str, bool]] = []
            outcome = ToolOutcome("saved", True, changed=True)

            runner._handle_project_tool_event(
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

    def test_trace_failures_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            runner = self._runner(state)

            class _ExplodingTrace:
                def record_analysis_run(self, _payload):
                    raise RuntimeError("trace disk full")

            work = _RunWork(recent_events=[], evidence=ExecutionEvidence(), trace=_ExplodingTrace())

            runner._handle_project_tool_event(
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
                def record_run(self, item, *, command, cwd, ok):
                    record("run", (command, cwd, ok))

                def record_edit(self, item, rel):
                    record("edit", (rel, "", True))

            fn(_Store(), object())

        return update_checkpoint

    def _runner(self, state: server.State) -> TaskRunner:
        return TaskRunner(
            state,
            agent_run=mock.Mock(return_value=RunResult("done", "done", 1)),
            collect_changes=mock.Mock(return_value={
                "ok": True,
                "changed_count": 0,
                "files": [],
                "diff": "",
            }),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _project: True,
        )


if __name__ == "__main__":
    unittest.main()
