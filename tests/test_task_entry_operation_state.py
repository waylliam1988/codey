"""Task entry commits its completion/repair lifecycle to runtime operation state.

These tests prove the durable operation contract end to end: the leaves a
project run passes through are visible on the durable operation state while the
run is alive, the terminal snapshot agrees with the final event and run ledger,
and the payload never carries raw prompt, diff, or failure output.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from codey.app import server
from codey.agents.request import AgentRequest
from codey.agents.runner import RunResult
from codey.completion import engine as completion_engine_module
from codey.completion.decision import (
    BLOCKED_MAX_REPAIR_ROUNDS,
    BLOCKED_UNOBSERVED,
    completion_blocked_reason,
)
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.runtime.operation_state import (
    LEAF_ACCEPTED,
    LEAF_COMPLETION_PROOF_RECORDED,
    LEAF_REPAIR_CONTEXT_ADMITTED,
    LEAF_REPAIR_RUNNING,
    LEAF_REPAIR_SETTLED,
    LEAF_TERMINAL,
    LEAF_WRITER_RUNNING,
    LEAF_WRITER_SETTLED,
    RuntimeOperationStore,
    RuntimeOperationTransitionError,
    lane_for_run,
    operation_id_for_run,
)
from codey.runs.details import load_run_details
from codey.runs.ledger import read_ledger
from codey.runtime.events import RunEvent
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.models import ToolCall
from codey.runtime.session_projection import reduce_session
from codey.runtime.session_log import RuntimeSessionLog
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.task.model import TaskSubmission
from codey.operations.project_completion_flow import COMPLETION_REPAIR_FOLLOWUP
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from codey.toolchain.runtime import ToolOutcome


SESSION = "s-opstate"
RESEARCH_ITERATION = "codey.operations.research_flow.run_research_iteration"


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self) -> None:
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout, text
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        self.closed = True


def _changes(*files: str) -> dict:
    diff = "".join(
        f"diff --git a/{item} b/{item}\n--- a/{item}\n+++ b/{item}\n@@ -1 +1 @@\n-before\n+after\n" for item in files
    )
    return {
        "ok": True,
        "changed_count": len(files),
        "files": [{"path": item, "status": "modified"} for item in files],
        "diff": diff,
        "mode": "git",
    }


class ObservingWriter:
    """Scripted writer that inspects the operation state mid-run."""

    def __init__(self, *steps: object) -> None:
        self.calls: list[dict[str, Any]] = []
        self.observed_phases: list[str | None] = []
        self.steps = list(steps)

    def __call__(self, request: AgentRequest, *, state_ref: server.AppContext) -> RunResult:
        self.calls.append({"task": request.task, "request": request})
        state = state_ref
        run_id = state.run_registry.current().run_id if state.run_registry.current() is not None else ""
        store = state.runtime_operations
        observed = store.load(SESSION, run_id) if store is not None else None
        self.observed_phases.append(observed.leaf if observed is not None else None)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        events, result = step
        for event in events:
            request.on_event(event)
        return result


def _edit_event(path: str = "src/mod.py") -> RunEvent:
    return RunEvent.tool_finished(
        1,
        ToolCall("edit", {"path": path, "old_string": "1", "new_string": "2"}),
        ToolOutcome("edited", True, changed=True),
    )


def _run_event(ok: bool) -> RunEvent:
    outcome = (
        ToolOutcome("all passed", True, exit_code=0)
        if ok
        else ToolOutcome(
            "1 failed\nFAILED tests/test_mod.py - assert 1 == 2",
            False,
            exit_code=1,
        )
    )
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": "python -m pytest", "path": "."}),
        outcome,
    )


def _pytest_project(td: Path) -> Path:
    project = Path(td) / "project"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    return project


def _runner(
    state: server.AppContext,
    writer: ObservingWriter,
    *,
    agent_run=None,
) -> TaskRunDeps:
    return TaskRunDeps(state=state,
        agent_run=agent_run or writer,
        collect_changes=mock.Mock(side_effect=lambda *_a, **_k: _changes("src/mod.py")),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
    )


def _run(
    runner: TaskRunDeps,
    state: server.AppContext,
    project: Path,
    writer: ObservingWriter,
    *,
    run_id: str = "",
) -> dict:
    def observed_agent_run(request: AgentRequest):
        return writer(request, state_ref=state)

    runner = replace(runner, agent_run=observed_agent_run)
    with mock.patch.object(state, "get_provider", return_value=_Provider()):
        run_task_submission(runner, 
            TaskSubmission(
                SESSION,
                str(project),
                "Change the module and verify",
                6,
                False,
                "deepseek",
                intent="project",
                run_id=run_id,
            )
        )
    return dict(state.run_registry.last_terminal_event())


def _operation(state: server.AppContext, run_id: str):
    assert state.runtime_operations is not None
    operation = state.runtime_operations.load(SESSION, run_id)
    assert operation is not None
    return operation


def _ledger_run_finished(state: server.AppContext, run_id: str) -> dict:
    assert state.run_ledgers is not None
    rows = [
        record.payload
        for record in read_ledger(state.run_ledgers.path_for(SESSION, run_id))
        if record.payload.get("type") == "run_finished"
    ]
    assert rows
    return rows[-1]


def _runtime_outcome(state: server.AppContext, run_id: str) -> str:
    assert state.runtime_log is not None
    projection = reduce_session(state.runtime_log.read(SESSION))
    return projection.operations[operation_id_for_run(run_id)].outcome


def _operation_leaf_sequence(state: server.AppContext, run_id: str) -> list[str]:
    assert state.runtime_log is not None
    op_id = operation_id_for_run(run_id)
    return [
        str(entry.payload["leaf"])
        for entry in state.runtime_log.read(SESSION)
        if entry.operation_id == op_id and entry.kind == "operation_state"
    ]


class CleanRunTerminalTests(unittest.TestCase):
    def test_clean_success_commits_terminal_matching_event_and_ledger(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            operation = _operation(state, run_id)

            self.assertEqual(operation.leaf, LEAF_TERMINAL)
            self.assertEqual(writer.observed_phases, [LEAF_WRITER_RUNNING])
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "done")
            self.assertEqual(operation.terminal.turns, 3)
            self.assertEqual(operation.terminal.max_turns, 6)
            self.assertEqual(operation.terminal.provider, "deepseek")
            self.assertEqual(operation.terminal.blocked_reason, "")
            # Terminal agrees with the user-visible event...
            self.assertEqual(operation.terminal.stop_reason, event["stop_reason"])
            self.assertEqual(operation.terminal.turns, event["turns"])
            # ...and with the ledger's run_finished row.
            finished = _ledger_run_finished(state, run_id)
            self.assertEqual(operation.terminal.stop_reason, finished["stop_reason"])
            self.assertEqual(operation.terminal.turns, finished["turns"])
            self.assertEqual(operation.terminal.provider, finished["provider"])
            self.assertEqual(
                operation.terminal.summary_chars,
                len(str(event.get("summary") or "")),
            )
            self.assertIs(operation.completion_proof_satisfied, True)
            assert state.runtime_log is not None
            runtime_starts = [
                entry
                for entry in state.runtime_log.read(SESSION)
                if entry.kind == "operation_started"
                and entry.operation_id == operation_id_for_run(run_id)
            ]
            self.assertEqual(
                [entry.operation_id for entry in runtime_starts],
                [operation_id_for_run(run_id)],
            )
            self.assertFalse(
                [
                    entry.operation_id
                    for entry in state.runtime_log.read(SESSION)
                    if entry.operation_id.startswith("runtime:")
                ]
            )
            projection = reduce_session(state.runtime_log.read(SESSION))
            self.assertEqual(
                projection.lanes[lane_for_run(run_id)].open_operation_id,
                "",
            )
            self.assertEqual(_runtime_outcome(state, run_id), "completed")

    def test_resume_same_run_continues_existing_task_operation(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("resumed", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            project = _pytest_project(root)
            state_home = root / "state"
            run_id = "resume-run-1"

            crashed_state = server.AppContext(state_home)
            reserved = crashed_state.reserve_run(
                session_id=SESSION,
                project=str(project),
                task="Change the module and verify",
                provider_id="deepseek",
            )
            assert reserved is not None
            self.assertTrue(
                crashed_state.replace_reserved_run(
                    reserved.run_id,
                    replace(reserved, run_id=run_id),
                )
            )
            assert crashed_state.runtime_mutations is not None
            started = crashed_state.runtime_mutations.accept_operation(
                session_id=SESSION,
                run_id=run_id,
                project=str(project),
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
                task_kind="project",
            )
            assert started is not None
            crashed_state.runtime_mutations.mark_writer_running(
                SESSION,
                run_id,
                provider_id="deepseek",
            )
            crashed_state.release_run(run_id)

            resumed_state = server.AppContext(state_home)
            event = _run(
                _runner(resumed_state, writer),
                resumed_state,
                project,
                writer,
                run_id=run_id,
            )

            assert resumed_state.runtime_log is not None
            entries = resumed_state.runtime_log.read(SESSION)
            op_id = operation_id_for_run(run_id)
            self.assertEqual(
                [
                    entry.payload["leaf"]
                    for entry in entries
                    if entry.operation_id == op_id
                    and entry.kind == "operation_state"
                ],
                [
                    LEAF_ACCEPTED,
                    LEAF_WRITER_RUNNING,
                    LEAF_WRITER_SETTLED,
                    LEAF_COMPLETION_PROOF_RECORDED,
                    LEAF_TERMINAL,
                ],
            )
            self.assertEqual(
                [entry.kind for entry in entries if entry.operation_id == op_id].count(
                    "operation_started"
                ),
                1,
            )
            self.assertEqual(
                [entry.kind for entry in entries if entry.operation_id == op_id].count(
                    "operation_settled"
                ),
                1,
            )
            self.assertEqual(writer.observed_phases, [LEAF_WRITER_RUNNING])
            self.assertEqual(event["stop_reason"], "done")
            self.assertEqual(_operation(resumed_state, run_id).leaf, LEAF_TERMINAL)


class RuntimeEnvelopeTests(unittest.TestCase):
    def test_runtime_start_failure_releases_reserved_slot(self) -> None:
        writer = ObservingWriter()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            runner = _runner(state, writer)
            assert state.runtime_log is not None

            with (
                mock.patch.object(
                    state.runtime_log,
                    "mutate",
                    side_effect=RuntimeError("runtime log unavailable"),
                ),
                self.assertRaises(RuntimeError),
            ):
                run_task_submission(runner, 
                    TaskSubmission(
                        SESSION,
                        str(project),
                        "Change the module and verify",
                        6,
                        False,
                        "deepseek",
                        intent="project",
                    )
                )

            self.assertIsNone(state.current_run())
            self.assertFalse(state.is_busy())

    def test_writer_running_mutation_failure_stops_before_agent_run(self) -> None:
        writer = ObservingWriter(
            ([], RunResult("should not run", "done", 1)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")

            assert state.runtime_mutations is not None
            with mock.patch.object(
                state.runtime_mutations,
                "mark_writer_running",
                side_effect=RuntimeOperationTransitionError("disk full"),
            ):
                event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            self.assertEqual(writer.calls, [])
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("mark_writer_running", str(event["summary"]))
            self.assertEqual(
                _operation_leaf_sequence(state, run_id),
                [LEAF_ACCEPTED, LEAF_TERMINAL],
            )
            self.assertEqual(_operation(state, run_id).leaf, LEAF_TERMINAL)
            self.assertFalse(state.is_busy())

    def test_writer_settled_mutation_failure_skips_completion_enforcement(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")

            assert state.runtime_mutations is not None
            with mock.patch.object(
                state.runtime_mutations,
                "mark_writer_settled",
                side_effect=RuntimeOperationTransitionError("disk full"),
            ):
                event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("mark_writer_settled", str(event["summary"]))
            self.assertEqual(
                _operation_leaf_sequence(state, run_id),
                [LEAF_ACCEPTED, LEAF_WRITER_RUNNING, LEAF_TERMINAL],
            )
            self.assertEqual(_operation(state, run_id).leaf, LEAF_TERMINAL)

    def test_completion_proof_mutation_failure_skips_repair(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([_edit_event(), _run_event(True)], RunResult("fixed", "done", 1)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")

            assert state.runtime_mutations is not None
            with mock.patch.object(
                state.runtime_mutations,
                "record_completion_proof",
                side_effect=RuntimeOperationTransitionError("disk full"),
            ):
                event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("record_completion_proof", str(event["summary"]))
            self.assertEqual(
                _operation_leaf_sequence(state, run_id),
                [
                    LEAF_ACCEPTED,
                    LEAF_WRITER_RUNNING,
                    LEAF_WRITER_SETTLED,
                    LEAF_TERMINAL,
                ],
            )
            self.assertEqual(_operation(state, run_id).leaf, LEAF_TERMINAL)

    def test_repair_running_mutation_failure_stops_before_repair_agent_run(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([_edit_event(), _run_event(True)], RunResult("fixed", "done", 1)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")

            assert state.runtime_mutations is not None
            with mock.patch.object(
                state.runtime_mutations,
                "mark_repair_running",
                side_effect=RuntimeOperationTransitionError("disk full"),
            ):
                event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("mark_repair_running", str(event["summary"]))
            self.assertEqual(
                _operation_leaf_sequence(state, run_id),
                [
                    LEAF_ACCEPTED,
                    LEAF_WRITER_RUNNING,
                    LEAF_WRITER_SETTLED,
                    LEAF_COMPLETION_PROOF_RECORDED,
                    LEAF_REPAIR_CONTEXT_ADMITTED,
                    LEAF_TERMINAL,
                ],
            )
            self.assertEqual(_operation(state, run_id).leaf, LEAF_TERMINAL)

    def test_research_terminal_uses_request_budget_for_runtime_phase(self) -> None:
        writer = ObservingWriter()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = server.AppContext(Path(td) / "state")
            runner = _runner(state, writer)
            runner = replace(runner, search_factory=lambda: object())
            result = ResearchRunResult(
                "question",
                "summary",
                "done",
                1,
                max_turns_used=1,
            )

            research_iteration = mock.Mock(
                return_value=ResearchIterationRun(result=result),
            )
            with (
                mock.patch.object(state, "get_provider", return_value=_Provider()),
                mock.patch(RESEARCH_ITERATION, research_iteration),
            ):
                run_task_submission(runner, 
                    TaskSubmission(
                        SESSION,
                        "",
                        "Research storage",
                        8,
                        False,
                        "deepseek",
                        intent="research",
                    )
                )

            event = dict(state.run_registry.last_terminal_event())
            operation = _operation(state, str(event["run_id"]))
            runtime_outcome = _runtime_outcome(state, str(event["run_id"]))

        self.assertEqual(event["max_turns"], 8)
        assert operation.terminal is not None
        self.assertEqual(operation.terminal.max_turns, 8)
        self.assertEqual(runtime_outcome, "completed")

    def test_hybrid_research_failure_uses_request_budget_for_runtime_phase(self) -> None:
        writer = ObservingWriter()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            project = _pytest_project(root)
            state = server.AppContext(root / "state")
            runner = _runner(state, writer)
            runner = replace(runner, search_factory=lambda: object())
            result = ResearchRunResult(
                "question",
                "need more evidence",
                "stopped",
                1,
                max_turns_used=1,
            )

            research_iteration = mock.Mock(
                return_value=ResearchIterationRun(result=result),
            )
            with (
                mock.patch.object(state, "get_provider", return_value=_Provider()),
                mock.patch(RESEARCH_ITERATION, research_iteration),
            ):
                run_task_submission(runner, 
                    TaskSubmission(
                        SESSION,
                        str(project),
                        "Research storage and update docs",
                        8,
                        False,
                        "deepseek",
                        intent="hybrid",
                    )
                )

            event = dict(state.run_registry.last_terminal_event())
            operation = _operation(state, str(event["run_id"]))
            runtime_outcome = _runtime_outcome(state, str(event["run_id"]))

        self.assertEqual(event["stop_reason"], "stopped")
        self.assertEqual(event["max_turns"], 8)
        assert operation.terminal is not None
        self.assertEqual(operation.terminal.max_turns, 8)
        self.assertEqual(runtime_outcome, "aborted")


class InvalidRuntimeProofWiringTests(unittest.TestCase):
    def test_fake_proof_with_int_satisfied_fails_closed(self) -> None:
        # Task entry passes the proof's facts through uncoerced so the strict
        # runtime helper validates them. A bad internal proof payload is a
        # runtime mutation failure, not a reason to continue without tracking.
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            real_build = completion_engine_module.build_completion_decision

            def build_with_int_satisfied(**kwargs):
                decision = real_build(**kwargs)
                if decision.proof is not None:
                    decision = replace(decision, proof=replace(decision.proof, satisfied=1))
                return decision

            with mock.patch.object(
                completion_engine_module, "build_completion_decision", build_with_int_satisfied
            ):
                event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("record_completion_proof", str(event["summary"]))
            self.assertEqual(operation.leaf, LEAF_TERMINAL)
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "error")
            self.assertIsNone(operation.completion_proof_satisfied)


class RepairRoundPhaseTests(unittest.TestCase):
    def test_repair_success_records_round_and_final_proof(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([_edit_event(), _run_event(True)], RunResult("fixed now", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            # The repair attempt ran while the counter said repair_running.
            self.assertEqual(writer.observed_phases, [LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING])
            self.assertEqual(len(writer.calls), 2)
            self.assertEqual(writer.calls[1]["task"], COMPLETION_REPAIR_FOLLOWUP)
            self.assertEqual(operation.repair_rounds, 1)
            self.assertTrue(operation.repair_context_ref.startswith("sha256:"))
            self.assertIs(operation.completion_proof_satisfied, True)
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "done")
            self.assertEqual(operation.terminal.blocked_reason, "")

    def test_repair_provider_failure_blocks_honestly(self) -> None:
        failure = ProviderFailure(
            model="DeepSeek Web",
            action="send",
            url="https://chat.deepseek.com/",
            title="failed",
            message="page exploded",
            time="2026-01-01T00:00:00Z",
        )
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ProviderActionError(failure),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            # One provider only: the failed repair round cannot switch, so
            # the ProviderActionError surfaces immediately.
            state.provider_failover_order = lambda: ("deepseek",)
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(event["stop_reason"], "blocked")
            self.assertEqual(writer.observed_phases, [LEAF_WRITER_RUNNING, LEAF_REPAIR_RUNNING])
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "blocked")
            self.assertEqual(operation.terminal.blocked_reason, "provider_failure")
            self.assertEqual(operation.repair_rounds, 1)
            self.assertEqual(operation.leaf, LEAF_TERMINAL)
            self.assertEqual(_runtime_outcome(state, str(event["run_id"])), "failed")

    def test_user_stop_during_repair_keeps_stopped_terminal(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([], RunResult("stopped", "stopped", 1)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(event["stop_reason"], "stopped")
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "stopped")
            self.assertEqual(operation.terminal.blocked_reason, "")
            self.assertNotIn("Completion blocked", str(event["summary"]))
            self.assertEqual(_runtime_outcome(state, str(event["run_id"])), "aborted")

    def test_still_failing_after_repair_names_max_repair_rounds(self) -> None:
        # The repair round runs and the budget is not the binding constraint
        # afterwards: the blocked note must name the round limit, not the
        # budget. (When the repair itself consumes the last turns, the
        # preserved 0.4.13 vocabulary says turn_budget_exhausted instead.)
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 3)),
            ([_edit_event(), _run_event(False)], RunResult("still broken", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(event["stop_reason"], "blocked")
            self.assertIn("after the repair round", str(event["summary"]))
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.blocked_reason, BLOCKED_MAX_REPAIR_ROUNDS)
            self.assertEqual(operation.completion_proof_status, "failed")

    def test_unobserved_proof_blocks_without_any_repair(self) -> None:
        writer = ObservingWriter(
            ([_edit_event()], RunResult("trust me", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(len(writer.calls), 1)
            self.assertEqual(event["stop_reason"], "blocked")
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.blocked_reason, BLOCKED_UNOBSERVED)
            self.assertEqual(operation.repair_rounds, 0)
            self.assertEqual(operation.repair_context_ref, "")


class CrashPositionTests(unittest.TestCase):
    def test_every_crash_position_reads_back_with_honest_progress(self) -> None:
        positions = [
            (
                [lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek")],
                LEAF_WRITER_RUNNING,
                "Writing was interrupted",
            ),
            (
                [
                    lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_writer_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                ],
                LEAF_WRITER_SETTLED,
                "Completion check was interrupted",
            ),
            (
                [
                    lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_writer_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    lambda line: line.record_completion_proof(
                        SESSION,
                        "run-crash",
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                ],
                LEAF_COMPLETION_PROOF_RECORDED,
                "Completion check was interrupted",
            ),
            (
                [
                    lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_writer_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    lambda line: line.record_completion_proof(
                        SESSION,
                        "run-crash",
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    lambda line: line.admit_repair_context(
                        SESSION,
                        "run-crash",
                        context_ref="sha256:" + "a" * 64,
                    ),
                    lambda line: line.mark_repair_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_repair_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        stop_reason="done",
                        turns_used=6,
                    ),
                ],
                LEAF_REPAIR_SETTLED,
                # The repair is over; what was interrupted is the
                # post-repair completion check.
                "Completion check was interrupted",
            ),
            (
                [
                    lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_writer_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    lambda line: line.record_completion_proof(
                        SESSION,
                        "run-crash",
                        proof_ref="completion_proof:" + "a" * 16,
                        proof_status="complete",
                        proof_satisfied=True,
                    ),
                ],
                LEAF_COMPLETION_PROOF_RECORDED,
                # A satisfied proof means the run was finishing, not
                # still waiting on a check.
                "Finishing was interrupted",
            ),
            (
                [
                    lambda line: line.mark_writer_running(SESSION, "run-crash", provider_id="deepseek"),
                    lambda line: line.mark_writer_settled(
                        SESSION,
                        "run-crash",
                        provider_id="deepseek",
                        turns_used=4,
                        stop_reason="done",
                    ),
                    lambda line: line.record_completion_proof(
                        SESSION,
                        "run-crash",
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    lambda line: line.admit_repair_context(
                        SESSION,
                        "run-crash",
                        context_ref="sha256:" + "a" * 64,
                    ),
                    lambda line: line.mark_repair_running(SESSION, "run-crash", provider_id="deepseek"),
                ],
                LEAF_REPAIR_RUNNING,
                "Stopped during repair",
            ),
        ]
        for commits, expected_phase, expected_text in positions:
            with self.subTest(phase=expected_phase):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    state_home = Path(td) / "state"
                    log = RuntimeSessionLog(state_home)
                    store = RuntimeOperationStore(log)
                    line = RuntimeMutationLine(log)
                    started = line.accept_operation(
                        session_id=SESSION,
                        run_id="run-crash",
                        project=str(Path(td) / "project"),
                        provider_id="deepseek",
                        turn_budget=6,
                        max_repair_rounds=1,
                    )
                    assert started is not None
                    for commit in commits:
                        # A crash means no further commits happen; the last
                        # committed phase must survive on disk.
                        current = store.load(SESSION, "run-crash")
                        assert current is not None
                        commit(line)

                    # The run opened a ledger but never wrote run_finished.
                    ledgers = RuntimeOperationLedgersStub(state_home)
                    ledgers.open_interrupted(SESSION, "run-crash")

                    # A fresh process would build a fresh store.
                    recovered_store = RuntimeOperationStore(RuntimeSessionLog(state_home))
                    recovered = recovered_store.load(SESSION, "run-crash")
                    assert recovered is not None
                    self.assertEqual(recovered.leaf, expected_phase)

                    summary = load_run_details(
                        run_ledgers=ledgers,
                        run_traces=None,
                        runtime_operations=recovered_store,
                        session_id=SESSION,
                        run_id="run-crash",
                    )
                    progress = [row.to_jsonable() for row in summary.rows if row.label == "Progress"]
                    self.assertEqual(len(progress), 1)
                    self.assertEqual(progress[0]["value"], expected_text)

    def test_stale_non_terminal_snapshot_never_shows_progress_after_finished_run(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            state_home = Path(td) / "state"
            log = RuntimeSessionLog(state_home)
            store = RuntimeOperationStore(log)
            line = RuntimeMutationLine(log)
            started = line.accept_operation(
                session_id=SESSION,
                run_id="run-stale",
                project="",
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
            )
            assert started is not None
            line.mark_writer_running(
                SESSION,
                "run-stale",
                provider_id="deepseek",
            )

            ledgers = RuntimeOperationLedgersStub(state_home)
            writer = ledgers.writer(SESSION, "run-stale")
            writer.finish(
                summary="done",
                stop_reason="done",
                turns=2,
                max_turns=6,
                provider="deepseek",
            )

            summary = load_run_details(
                run_ledgers=ledgers,
                run_traces=None,
                runtime_operations=store,
                session_id=SESSION,
                run_id="run-stale",
            )
            self.assertEqual(
                [row.label for row in summary.rows if row.label == "Progress"],
                [],
            )

    def test_terminal_operation_shows_no_progress_line(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            state_home = Path(td) / "state"
            log = RuntimeSessionLog(state_home)
            store = RuntimeOperationStore(log)
            line = RuntimeMutationLine(log)
            started = line.accept_operation(
                session_id=SESSION,
                run_id="run-done",
                project="",
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
            )
            assert started is not None
            line.mark_terminal(
                SESSION,
                "run-done",
                stop_reason="done",
                summary_chars=3,
                turns=2,
                max_turns=6,
                provider="deepseek",
            )

            summary = load_run_details(
                run_ledgers=RuntimeOperationLedgersStub(state_home),
                run_traces=None,
                runtime_operations=store,
                session_id=SESSION,
                run_id="run-done",
            )
            self.assertEqual(
                [row.label for row in summary.rows if row.label == "Progress"],
                [],
            )


class RuntimeOperationLedgersStub:
    """Minimal ledger-store stand-in for details-only tests."""

    def __init__(self, state_home: Path) -> None:
        from codey.runs.ledger import RunLedgerStore

        self._store = RunLedgerStore(state_home)

    def path_for(self, session_id: str, run_id: str) -> Path:
        return self._store.path_for(session_id, run_id)

    def writer(self, session_id: str, run_id: str):
        from codey.runs.ledger import RunLedgerWriter

        return RunLedgerWriter(
            self._store.path_for(session_id, run_id),
            run_id=run_id,
            session_id=session_id,
        )

    def open_interrupted(self, session_id: str, run_id: str) -> None:
        # Mirror RunLedgerStore.open(): an interrupted run has a started
        # ledger and no run_finished row.
        self._store.open(
            run_id=run_id,
            session_id=session_id,
            project="",
            task="",
            provider="deepseek",
            mode="project",
        )


class PayloadHygieneTests(unittest.TestCase):
    def test_operation_payload_never_contains_repair_prompt_or_failure_output(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([_edit_event(), _run_event(False)], RunResult("still broken", "done", 2)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            _run(_runner(state, writer), state, project, writer)

            assert state.runtime_log is not None
            raw = state.runtime_log.path_for(SESSION).read_text(encoding="utf-8")

            for fragment in (
                "Change the module and verify",  # owner task
                COMPLETION_REPAIR_FOLLOWUP[:60],  # repair prompt text
                "FAILED tests/test_mod.py",  # failing check output
                "diff --git",  # raw diff
                "old_string",  # tool args
                "trust me",
            ):
                self.assertNotIn(fragment, raw)
            self.assertNotIn("prompt", raw)

    def test_operation_payload_never_contains_the_raw_project_path(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.AppContext(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            assert state.runtime_log is not None
            raw = state.runtime_log.path_for(SESSION).read_text(encoding="utf-8")

            # The roadmap requires a ref, never the raw absolute path; the
            # resolved temp project path must not appear in any form.
            self.assertNotIn(str(project), raw)
            self.assertNotIn(str(project).replace("\\", "/"), raw)
            operation = _operation(state, str(event["run_id"]))
            self.assertRegex(operation.project_ref, r"^project:[0-9a-f]{24}$")


class BlockedReasonProjectionTests(unittest.TestCase):
    def test_completion_blocked_reason_vocabulary_is_the_documented_chain(self) -> None:
        self.assertEqual(
            completion_blocked_reason(
                proof_status="blocked",
                failure_class="product_failure",
                remaining_turns=2,
                repair_rounds=0,
            ),
            BLOCKED_UNOBSERVED,
        )
        self.assertEqual(
            completion_blocked_reason(
                proof_status="failed",
                failure_class="environment_failure",
                remaining_turns=2,
                repair_rounds=0,
            ),
            "environment_failure",
        )
        self.assertEqual(
            completion_blocked_reason(
                proof_status="failed",
                failure_class="product_failure",
                remaining_turns=0,
                repair_rounds=0,
            ),
            "turn_budget_exhausted",
        )
        self.assertEqual(
            completion_blocked_reason(
                proof_status="failed",
                failure_class="product_failure",
                remaining_turns=2,
                repair_rounds=1,
            ),
            BLOCKED_MAX_REPAIR_ROUNDS,
        )
        self.assertEqual(
            completion_blocked_reason(
                proof_status="failed",
                failure_class="product_failure",
                remaining_turns=2,
                repair_rounds=0,
            ),
            "repair_not_admitted",
        )


if __name__ == "__main__":
    unittest.main()
