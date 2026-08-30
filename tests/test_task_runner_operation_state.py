"""TaskRunner commits its completion/repair lifecycle to RunOperationStore.

These tests prove the 0.5.1 contract end to end: the phases a project run
passes through are visible on the durable counter while the run is alive,
the terminal snapshot agrees with the final event and the run ledger, and
the payload never carries raw prompt, diff, or failure output.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from codey.app import server, task_runner as task_runner_module
from codey.agents.runner import RunResult
from codey.completion.decision import (
    BLOCKED_MAX_REPAIR_ROUNDS,
    BLOCKED_UNOBSERVED,
    completion_blocked_reason,
)
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.run_operation import (
    PHASE_COMPLETION_PROOF_RECORDED,
    PHASE_REPAIR_RUNNING,
    PHASE_REPAIR_SETTLED,
    PHASE_TERMINAL,
    PHASE_WRITER_RUNNING,
    PHASE_WRITER_SETTLED,
    RunOperationStore,
    mark_completion_proof_recorded,
    mark_repair_context_admitted,
    mark_repair_running,
    mark_repair_settled,
    mark_terminal,
    mark_writer_running,
    mark_writer_settled,
)
from codey.runs.details import load_run_details
from codey.runs.ledger import read_ledger
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall
from codey.app.task_runner import (
    COMPLETION_REPAIR_FOLLOWUP,
    TaskRequest,
    TaskRunner,
)
from codey.toolchain.runtime import ToolOutcome


SESSION = "s-opstate"


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

    def __call__(self, _provider, _project, task, **kwargs) -> RunResult:
        self.calls.append({"task": task, "kwargs": kwargs})
        state = kwargs["state_ref"]
        run_id = state.active_run.run_id if state.active_run is not None else ""
        store = state.run_operations
        observed = store.load(SESSION, run_id) if store is not None else None
        self.observed_phases.append(observed.phase if observed is not None else None)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        events, result = step
        for event in events:
            kwargs["on_event"](event)
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


def _runner(state: server.State, writer: ObservingWriter) -> TaskRunner:
    return TaskRunner(
        state,
        agent_run=writer,
        collect_changes=mock.Mock(side_effect=lambda *_a, **_k: _changes("src/mod.py")),
        run_review=mock.Mock(return_value=None),
        capture_provider_failure=server.capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        run_operations=state.run_operations,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=lambda _project: True,
    )


def _run(runner: TaskRunner, state: server.State, project: Path, writer: ObservingWriter) -> dict:
    def observed_agent_run(provider, project_path, task, **kwargs):
        return writer(provider, project_path, task, state_ref=state, **kwargs)

    runner.agent_run = observed_agent_run
    with mock.patch.object(state, "get_provider", return_value=_Provider()):
        runner.run(
            TaskRequest(
                SESSION,
                str(project),
                "Change the module and verify",
                6,
                False,
                "deepseek",
                intent="project",
            )
        )
    return dict(state.last_terminal_event)


def _operation(state: server.State, run_id: str):
    assert state.run_operations is not None
    operation = state.run_operations.load(SESSION, run_id)
    assert operation is not None
    return operation


def _ledger_run_finished(state: server.State, run_id: str) -> dict:
    assert state.run_ledgers is not None
    rows = [
        record.payload
        for record in read_ledger(state.run_ledgers.path_for(SESSION, run_id))
        if record.payload.get("type") == "run_finished"
    ]
    assert rows
    return rows[-1]


class CleanRunTerminalTests(unittest.TestCase):
    def test_clean_success_commits_terminal_matching_event_and_ledger(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.State(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            run_id = str(event["run_id"])
            operation = _operation(state, run_id)

            self.assertEqual(operation.phase, PHASE_TERMINAL)
            self.assertEqual(writer.observed_phases, [PHASE_WRITER_RUNNING])
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


class NonBoolSatisfiedWiringTests(unittest.TestCase):
    def test_fake_proof_with_int_satisfied_records_terminal_error(self) -> None:
        # TaskRunner passes the proof's facts through uncoerced: the strict
        # helper validates them. A proof carrying satisfied=1 (an int)
        # must not be swallowed by the operation writer; the run reaches a
        # visible terminal error instead of leaving the register silently stale.
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.State(Path(td) / "state")
            real_build = task_runner_module.build_completion_decision

            def build_with_int_satisfied(**kwargs):
                decision = real_build(**kwargs)
                if decision.proof is not None:
                    decision = replace(decision, proof=replace(decision.proof, satisfied=1))
                return decision

            with mock.patch.object(
                task_runner_module, "build_completion_decision", build_with_int_satisfied
            ):
                event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))
            self.assertEqual(event["stop_reason"], "error")
            self.assertIn("proof_satisfied must be a bool", str(event["summary"]))
            self.assertEqual(operation.phase, PHASE_TERMINAL)
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
            state = server.State(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            # The repair attempt ran while the counter said repair_running.
            self.assertEqual(writer.observed_phases, [PHASE_WRITER_RUNNING, PHASE_REPAIR_RUNNING])
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
            state = server.State(Path(td) / "state")
            # One provider only: the failed repair round cannot switch, so
            # the ProviderActionError surfaces immediately.
            state.provider_failover_order = lambda: ("deepseek",)
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(event["stop_reason"], "blocked")
            self.assertEqual(writer.observed_phases, [PHASE_WRITER_RUNNING, PHASE_REPAIR_RUNNING])
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "blocked")
            self.assertEqual(operation.terminal.blocked_reason, "provider_failure")
            self.assertEqual(operation.repair_rounds, 1)
            self.assertEqual(operation.phase, PHASE_TERMINAL)

    def test_user_stop_during_repair_keeps_stopped_terminal(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
            ([], RunResult("stopped", "stopped", 1)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.State(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            operation = _operation(state, str(event["run_id"]))

            self.assertEqual(event["stop_reason"], "stopped")
            assert operation.terminal is not None
            self.assertEqual(operation.terminal.stop_reason, "stopped")
            self.assertEqual(operation.terminal.blocked_reason, "")
            self.assertNotIn("Completion blocked", str(event["summary"]))

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
            state = server.State(Path(td) / "state")
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
            state = server.State(Path(td) / "state")
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
                [lambda s: mark_writer_running(s, provider_id="deepseek")],
                PHASE_WRITER_RUNNING,
                "Writing was interrupted",
            ),
            (
                [
                    lambda s: mark_writer_running(s, provider_id="deepseek"),
                    lambda s: mark_writer_settled(s, provider_id="deepseek", turns_used=4, stop_reason="done"),
                ],
                PHASE_WRITER_SETTLED,
                "Completion check was interrupted",
            ),
            (
                [
                    lambda s: mark_writer_running(s, provider_id="deepseek"),
                    lambda s: mark_writer_settled(s, provider_id="deepseek", turns_used=4, stop_reason="done"),
                    lambda s: mark_completion_proof_recorded(
                        s,
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                ],
                PHASE_COMPLETION_PROOF_RECORDED,
                "Completion check was interrupted",
            ),
            (
                [
                    lambda s: mark_writer_running(s, provider_id="deepseek"),
                    lambda s: mark_writer_settled(s, provider_id="deepseek", turns_used=4, stop_reason="done"),
                    lambda s: mark_completion_proof_recorded(
                        s,
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    lambda s: mark_repair_context_admitted(s, context_ref="sha256:" + "a" * 64),
                    lambda s: mark_repair_running(s, provider_id="deepseek"),
                    lambda s: mark_repair_settled(s, provider_id="deepseek", stop_reason="done", turns_used=6),
                ],
                PHASE_REPAIR_SETTLED,
                # The repair is over; what was interrupted is the
                # post-repair completion check.
                "Completion check was interrupted",
            ),
            (
                [
                    lambda s: mark_writer_running(s, provider_id="deepseek"),
                    lambda s: mark_writer_settled(s, provider_id="deepseek", turns_used=4, stop_reason="done"),
                    lambda s: mark_completion_proof_recorded(
                        s,
                        proof_ref="completion_proof:" + "a" * 16,
                        proof_status="complete",
                        proof_satisfied=True,
                    ),
                ],
                PHASE_COMPLETION_PROOF_RECORDED,
                # A satisfied proof means the run was finishing, not
                # still waiting on a check.
                "Finishing was interrupted",
            ),
            (
                [
                    lambda s: mark_writer_running(s, provider_id="deepseek"),
                    lambda s: mark_writer_settled(s, provider_id="deepseek", turns_used=4, stop_reason="done"),
                    lambda s: mark_completion_proof_recorded(
                        s,
                        proof_ref="completion_proof:" + "b" * 16,
                        proof_status="failed",
                        proof_satisfied=False,
                    ),
                    lambda s: mark_repair_context_admitted(s, context_ref="sha256:" + "a" * 64),
                    lambda s: mark_repair_running(s, provider_id="deepseek"),
                ],
                PHASE_REPAIR_RUNNING,
                "Stopped during repair",
            ),
        ]
        for transitions, expected_phase, expected_text in positions:
            with self.subTest(phase=expected_phase):
                with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                    state_home = Path(td) / "state"
                    store = RunOperationStore(state_home)
                    started = store.start(
                        session_id=SESSION,
                        run_id="run-crash",
                        project=str(Path(td) / "project"),
                        provider_id="deepseek",
                        turn_budget=6,
                        max_repair_rounds=1,
                    )
                    assert started is not None
                    for transition in transitions:
                        # A crash means no further commits happen; the last
                        # committed phase must survive on disk.
                        current = store.load(SESSION, "run-crash")
                        assert current is not None
                        store.commit(SESSION, "run-crash", transition)

                    # The run opened a ledger but never wrote run_finished.
                    ledgers = RunOperationLedgersStub(state_home)
                    ledgers.open_interrupted(SESSION, "run-crash")

                    # A fresh process would build a fresh store.
                    recovered_store = RunOperationStore(state_home)
                    recovered = recovered_store.load(SESSION, "run-crash")
                    assert recovered is not None
                    self.assertEqual(recovered.phase, expected_phase)

                    summary = load_run_details(
                        run_ledgers=ledgers,
                        run_traces=None,
                        run_operations=recovered_store,
                        session_id=SESSION,
                        run_id="run-crash",
                    )
                    progress = [row.to_jsonable() for row in summary.rows if row.label == "Progress"]
                    self.assertEqual(len(progress), 1)
                    self.assertEqual(progress[0]["value"], expected_text)

    def test_stale_non_terminal_snapshot_never_shows_progress_after_finished_run(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            state_home = Path(td) / "state"
            store = RunOperationStore(state_home)
            started = store.start(
                session_id=SESSION,
                run_id="run-stale",
                project="",
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
            )
            assert started is not None
            store.commit(
                SESSION,
                "run-stale",
                lambda s: mark_writer_running(s, provider_id="deepseek"),
            )

            ledgers = RunOperationLedgersStub(state_home)
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
                run_operations=store,
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
            store = RunOperationStore(state_home)
            started = store.start(
                session_id=SESSION,
                run_id="run-done",
                project="",
                provider_id="deepseek",
                turn_budget=6,
                max_repair_rounds=1,
            )
            assert started is not None
            store.commit(
                SESSION,
                "run-done",
                lambda s: mark_terminal(
                    s,
                    stop_reason="done",
                    summary_chars=3,
                    turns=2,
                    max_turns=6,
                    provider="deepseek",
                ),
            )

            summary = load_run_details(
                run_ledgers=RunOperationLedgersStub(state_home),
                run_traces=None,
                run_operations=store,
                session_id=SESSION,
                run_id="run-done",
            )
            self.assertEqual(
                [row.label for row in summary.rows if row.label == "Progress"],
                [],
            )


class RunOperationLedgersStub:
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
            state = server.State(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            assert state.run_operations is not None
            path = state.run_operations.path_for(SESSION, str(event["run_id"]))
            raw = path.read_text(encoding="utf-8")

            for fragment in (
                "Change the module and verify",  # owner task
                COMPLETION_REPAIR_FOLLOWUP[:60],  # repair prompt text
                "FAILED tests/test_mod.py",  # failing check output
                "diff --git",  # raw diff
                "old_string",  # tool args
                "trust me",
            ):
                self.assertNotIn(fragment, raw)
            payload = json.loads(raw)
            self.assertNotIn("prompt", json.dumps(payload))

    def test_operation_payload_never_contains_the_raw_project_path(self) -> None:
        writer = ObservingWriter(
            ([_edit_event(), _run_event(True)], RunResult("implemented", "done", 3)),
        )
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            project = _pytest_project(Path(td))
            state = server.State(Path(td) / "state")
            event = _run(_runner(state, writer), state, project, writer)

            assert state.run_operations is not None
            path = state.run_operations.path_for(SESSION, str(event["run_id"]))
            raw = path.read_text(encoding="utf-8")

            # The roadmap requires a ref, never the raw absolute path; the
            # resolved temp project path must not appear in any form.
            self.assertNotIn(str(project), raw)
            self.assertNotIn(str(project).replace("\\", "/"), raw)
            payload = json.loads(raw)
            self.assertRegex(payload["project_ref"], r"^project:[0-9a-f]{24}$")


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
