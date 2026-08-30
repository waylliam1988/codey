"""TaskService completion enforcement and one bounded repair round (0.4.13)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from codey.app import server
from codey.operations import task_flow as task_service_module
from codey.agents.runner import RunResult
from codey.completion.engine import COMPLETION_BLOCKED_NOTES
from codey.completion.decision import BLOCKED_TURN_BUDGET_EXHAUSTED
from codey.operations.task_flow import _blocked_result
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall
from codey.task.model import TaskSubmission
from codey.task.service import (
    COMPLETION_REPAIR_FOLLOWUP,
    TaskService,
)
from codey.toolchain.runtime import ToolOutcome
from codey.completion.verification_policy import VerificationCandidate


class _Provider:
    name = "DeepSeek Web"
    location = "https://chat.deepseek.com/"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = False

    def new_chat(self, timeout: float | None = None) -> None:
        del timeout

    def send(self, text: str, timeout: float | None = None) -> str:
        del timeout
        self.prompts.append(text)
        return '{"tool":"done","args":{"summary":"ok"}}'

    def close(self) -> None:
        self.closed = True


def _changes(*files: str) -> dict:
    diff = "".join(
        f"diff --git a/{item} b/{item}\n"
        f"--- a/{item}\n"
        f"+++ b/{item}\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
        for item in files
    )
    return {
        "ok": True,
        "changed_count": len(files),
        "files": [{"path": item, "status": "modified"} for item in files],
        "diff": diff,
        "mode": "git",
    }


class ScriptedWriter:
    """Stands in for agent.run; emits events then returns scripted results."""

    def __init__(self, *steps: tuple[list[RunEvent], RunResult]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, _provider, _project, task, **kwargs) -> RunResult:
        self.calls.append({"task": task, "kwargs": kwargs})
        events, result = self.steps.pop(0)
        for event in events:
            kwargs["on_event"](event)
        return result


def _edit_event(path: str = "src/mod.py") -> RunEvent:
    return RunEvent.tool_finished(
        1,
        ToolCall("edit", {"path": path, "old_string": "1", "new_string": "2"}),
        ToolOutcome("edited", True, changed=True),
    )


def _run_event(ok: bool, *, error_code: str = "") -> RunEvent:
    command = "python -m pytest"
    if ok:
        outcome = ToolOutcome("all passed", True, exit_code=0)
    else:
        outcome = ToolOutcome(
            "1 failed\nFAILED tests/test_mod.py - assert 1 == 2",
            False,
            exit_code=None if error_code else 1,
            error_code=error_code or ("error" if not ok else ""),
        )
        del command
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": "python -m pytest", "path": "."}),
        outcome,
    )


def _run_event_output(outcome: ToolOutcome) -> RunEvent:
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": "python -m pytest", "path": "."}),
        outcome,
    )


def _scoped_run_event(command: str, path: str, ok: bool) -> RunEvent:
    return RunEvent.tool_finished(
        2,
        ToolCall("run", {"command": command, "path": path}),
        ToolOutcome("passed" if ok else "failed", ok, exit_code=0 if ok else 1),
    )


def _runner(state: server.State, writer: ScriptedWriter) -> TaskService:
    return TaskService(
        state,
        agent_run=writer,
        collect_changes=mock.Mock(side_effect=lambda *_a, **_k: _changes("src/mod.py")),
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


def _pytest_project(td: Path) -> Path:
    project = Path(td) / "project"
    (project / "src").mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    return project


def _run(runner: TaskService, state: server.State, project: Path) -> dict:
    with mock.patch.object(state, "get_provider", return_value=_Provider()):
        runner.run(TaskSubmission(
            "s-enforce",
            str(project),
            "Change the module and verify",
            6,
            False,
            "deepseek",
            intent="project",
        ))
    return dict(state.last_terminal_event)


def _trace_payload(state: server.State) -> dict:
    assert state.run_traces is not None
    path = state.run_traces.path_for("s-enforce", state.last_terminal_event["run_id"])
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_pass_allows_done_with_verified_receipt() -> None:
    writer = ScriptedWriter((
        [_edit_event(), _run_event(True)],
        RunResult("implemented", "done", 3),
    ))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "done"
        receipt = event["receipt"]
        assert receipt["verification"]["checks_passed"] is True
        assert receipt["verification"]["trust"] == "trusted"
        assert receipt["display"]["summary"] == "1 file changed · checks passed"

        manifest = _trace_payload(state)
        proofs = manifest["completion_proofs"]
        assert len(proofs) == 1
        assert proofs[0]["status"] == "complete"
        assert proofs[0]["satisfied"] is True
        # A clean completion admits no repair context.
        assert manifest["completion_repair_context"] == []


def test_fresh_fail_runs_one_repair_round_then_completes() -> None:
    writer = ScriptedWriter(
        ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
        ([_edit_event(), _run_event(True)], RunResult("fixed now", "done", 2)),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 2
        repair_call = writer.calls[1]
        assert repair_call["task"] == COMPLETION_REPAIR_FOLLOWUP
        context_text = repair_call["kwargs"]["completion_repair_context"]
        assert "Completion repair context. Facts only." in context_text
        assert "python -m pytest" in context_text
        payload = repair_call["kwargs"]["completion_repair_context_payload"]
        assert payload["admitted"] is True
        assert payload["failure_class"] == "product_failure"

        assert event["stop_reason"] == "done"
        assert event["summary"] == "fixed now"
        assert event["receipt"]["verification"]["checks_passed"] is True

        manifest = _trace_payload(state)
        statuses = [row["status"] for row in manifest["completion_proofs"]]
        assert statuses == ["failed", "complete"]
        # The trace admission row itself is produced inside the real
        # agent.run send boundary (covered by the agent-level tests); the
        # mocked writer here proves the orchestration wiring: the payload
        # was handed to the writer attempt exactly once.
        raw = json.dumps(manifest, ensure_ascii=False)
        assert "FAILED tests/test_mod.py" not in raw


def test_repair_round_refreshes_verification_candidates_for_final_proof() -> None:
    writer = ScriptedWriter(
        (
            [
                _edit_event("frontend/src/app.ts"),
                _scoped_run_event("npm test", "frontend", False),
            ],
            RunResult("done?", "done", 3),
        ),
        (
            [
                _edit_event("backend/app.py"),
                _scoped_run_event("python -m pytest", "backend", True),
            ],
            RunResult("fixed now", "done", 2),
        ),
    )
    changes = [_changes("frontend/src/app.ts"), _changes("backend/app.py")]

    def collect(*_args, **_kwargs):
        return changes.pop(0) if changes else _changes("backend/app.py")

    def candidates(*_args, **_kwargs):
        if changes:
            return (VerificationCandidate("npm test", "frontend"),)
        return (VerificationCandidate("python -m pytest", "backend"),)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = Path(td) / "project"
        (project / "frontend" / "src").mkdir(parents=True)
        (project / "backend").mkdir(parents=True)
        state = server.State(Path(td) / "state")
        runner = TaskService(
            state,
            agent_run=writer,
            collect_changes=mock.Mock(side_effect=collect),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
        )
        with mock.patch.object(task_service_module, "safe_verification_candidates", candidates):
            event = _run(runner, state, project)

        assert len(writer.calls) == 2
        assert event["stop_reason"] == "done"
        assert event["receipt"]["verification"]["checks_passed"] is True
        statuses = [row["status"] for row in _trace_payload(state)["completion_proofs"]]
        assert statuses == ["failed", "complete"]


def test_still_failing_after_repair_round_blocks_honestly() -> None:
    writer = ScriptedWriter(
        ([_edit_event(), _run_event(False)], RunResult("done?", "done", 4)),
        ([_edit_event(), _run_event(False)], RunResult("still broken", "done", 2)),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 2
        assert event["stop_reason"] == "blocked"
        assert "[Completion blocked:" in event["summary"]
        assert event["receipt"]["verification"]["checks_passed"] is False

        manifest = _trace_payload(state)
        # Content-addressed proofs dedupe: identical failed facts before and
        # after the repair round are one proof. The blocked terminal event
        # and the second writer call prove the round actually ran.
        assert all(row["status"] == "failed" for row in manifest["completion_proofs"])
        # The durable ledger was written once, from the final outcome only.
        from codey.runs.ledger import read_ledger

        assert state.run_ledgers is not None
        records = read_ledger(
            state.run_ledgers.path_for("s-enforce", event["run_id"])
        )
        ledger_events = [
            record.payload
            for record in records
            if record.payload.get("type") == "changes_collected"
        ]
        assert len(ledger_events) == 1
        assert ledger_events[0]["receipt"]["verification"]["checks_passed"] is False


def test_unobserved_verification_blocks_without_any_repair() -> None:
    # The model claims done but never ran a check: unobserved is not a bug
    # report, so no repair context may be constructed either.
    writer = ScriptedWriter((
        [_edit_event()],
        RunResult("trust me", "done", 2),
    ))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        assert "[Completion blocked:" in event["summary"]
        assert event["receipt"]["verification"]["checks_passed"] is False

        manifest = _trace_payload(state)
        assert manifest["completion_proofs"][0]["status"] == "blocked"
        assert manifest["completion_repair_context"] == []


def test_forbidden_verification_allows_limited_done() -> None:
    writer = ScriptedWriter(([_edit_event()], RunResult("changed", "done", 2)))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        runner = _runner(state, writer)

        with mock.patch.object(state, "get_provider", return_value=_Provider()):
            runner.run(TaskSubmission(
                "s-enforce",
                str(project),
                "In src/mod.py change VALUE from 1 to 2. Do not run any commands; report done once edited.",
                6,
                False,
                "deepseek",
                intent="project",
            ))

        event = dict(state.last_terminal_event)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "done"
        assert "[Completion blocked:" not in str(event["summary"])
        assert event["receipt"]["verification"]["checks_passed"] is False

        manifest = _trace_payload(state)
        proof = manifest["completion_proofs"][0]
        assert proof["status"] == "complete_with_limitations"
        assert proof["limitation_refs"] == ["verification_forbidden_by_user"]
        assert proof["checks"] == [{
            "check_id": "relevant_verification",
            "status": "not_applicable",
            "reason_code": "verification_forbidden_by_user",
        }]
        assert manifest["completion_repair_context"] == []


def test_claim_only_pass_cannot_become_a_verified_receipt() -> None:
    # No candidate covers the change at all: even a claimed green must not
    # produce a verified receipt or a done result.
    changes = _changes("src/other.py")
    writer = ScriptedWriter((
        [],
        RunResult("claims green", "done", 1),
    ))

    def collect(*_args, **_kwargs):
        return changes

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = Path(td) / "project"
        (project / "src").mkdir(parents=True)
        state = server.State(Path(td) / "state")
        runner = TaskService(
            state,
            agent_run=writer,
            collect_changes=collect,
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
        )
        event = _run(runner, state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        assert event["receipt"]["verification"]["checks_passed"] is False


def test_environment_failure_blocks_without_repair() -> None:
    writer = ScriptedWriter(
        ([_edit_event(), _run_event(False, error_code="timeout")], RunResult("done?", "done", 3)),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        # The check could not even run: blocking is honest, repairing the
        # code would be wrong.
        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        manifest = _trace_payload(state)
        assert manifest["completion_repair_context"] == []


def test_dependency_failure_with_exit_one_blocks_without_repair() -> None:
    # "No module named pytest" exits 1 like a real product failure, but its
    # own output names the environment: no repair round may run for it.
    writer = ScriptedWriter((
        [_edit_event(), _run_event_output(
            ToolOutcome(
                "ERROR: No module named pytest\n"
                "ModuleNotFoundError: No module named 'pytest'",
                False,
                exit_code=1,
            )
        )],
        RunResult("unreachable", "done", 1),
    ))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        assert "environment error" in str(event["summary"])
        manifest = _trace_payload(state)
        assert manifest["completion_proofs"][0]["status"] == "failed"
        assert manifest["completion_repair_context"] == []


def test_exhausted_turn_budget_never_runs_an_extra_repair_turn() -> None:
    # The initial writer used the whole turn budget and still failed the
    # proof with a claimed done: no repair turn may physically exceed
    # max_turns, and the run must block honestly instead of clamping the
    # display back.
    writer = ScriptedWriter((
        [_edit_event(), _run_event(False)],
        RunResult("unreachable", "done", 6),
    ))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        assert "no turn budget remains" in str(event["summary"])
        assert event["receipt"]["verification"]["checks_passed"] is False


def test_unavailable_changes_with_observed_edits_stay_in_enforcement_scope() -> None:
    # Changes collection produced no usable verdict while real edits were
    # observed locally: enforcement scopes from the observed edits instead
    # of letting an edited run pass as an unverifiable done.
    unavailable_changes = {
        "ok": False,
        "error": "git status failed",
        "files": [],
        "diff": "",
    }
    writer = ScriptedWriter(([_edit_event()], RunResult("trust me", "done", 2)))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        runner = TaskService(
            state,
            agent_run=writer,
            collect_changes=mock.Mock(return_value=unavailable_changes),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
        )
        event = _run(runner, state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "blocked"
        assert "[Completion blocked:" in str(event["summary"])
        assert event["receipt"]["verification"]["checks_passed"] is False
        manifest = _trace_payload(state)
        assert manifest["completion_proofs"][0]["status"] == "blocked"


def test_measured_net_empty_diff_keeps_reverted_runs_out_of_scope() -> None:
    # The model edited and then reverted everything: the collected diff is
    # a real measurement reporting zero files, so the run is genuinely
    # unchanged -- out of enforcement scope, no blocked receipt, and the
    # observed-edit fallback must not override the measurement.
    empty_git_changes = {
        "ok": True,
        "changed_count": 0,
        "files": [],
        "diff": "",
        "mode": "git",
    }
    writer = ScriptedWriter(([_edit_event()], RunResult("reverted", "done", 2)))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        runner = TaskService(
            state,
            agent_run=writer,
            collect_changes=mock.Mock(return_value=empty_git_changes),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
        )
        event = _run(runner, state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "done"
        assert "[Completion blocked:" not in str(event["summary"])
        manifest = _trace_payload(state)
        assert manifest["completion_proofs"] == []


def test_docs_only_change_keeps_limited_done() -> None:
    docs_changes = {
        "ok": True,
        "changed_count": 1,
        "files": [{"path": "README.md", "status": "modified"}],
        "diff": "",
        "mode": "git",
    }
    writer = ScriptedWriter(([], RunResult("docs updated", "done", 1)))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = Path(td) / "project"
        (project / "src").mkdir(parents=True)
        (project / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n",
            encoding="utf-8",
        )
        state = server.State(Path(td) / "state")
        runner = TaskService(
            state,
            agent_run=writer,
            collect_changes=mock.Mock(return_value=docs_changes),
            run_review=mock.Mock(return_value=None),
            capture_provider_failure=server.capture_provider_failure,
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=lambda _p: True,
        )
        event = _run(runner, state, project)

        assert len(writer.calls) == 1
        assert event["stop_reason"] == "done"
        manifest = _trace_payload(state)
        proof = manifest["completion_proofs"][0]
        assert proof["status"] == "complete_with_limitations"
        assert proof["limitation_refs"] == ["docs_only_change"]


def test_repair_phase_ending_in_max_turns_becomes_blocked() -> None:
    writer = ScriptedWriter(
        ([_edit_event(), _run_event(False)], RunResult("done?", "done", 5)),
        ([], RunResult("out of turns", "max_turns", 1)),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 2
        assert event["stop_reason"] == "blocked"
        assert "no turn budget remains" in str(event["summary"])
        assert state.runtime_operations is not None
        operation = state.runtime_operations.load("s-enforce", event["run_id"])
        assert operation is not None
        assert operation.terminal is not None
        assert operation.terminal.blocked_reason == BLOCKED_TURN_BUDGET_EXHAUSTED


def test_user_stop_during_repair_stays_stopped_not_fake_done() -> None:
    writer = ScriptedWriter(
        ([_edit_event(), _run_event(False)], RunResult("done?", "done", 5)),
        ([], RunResult("stopped", "stopped", 1)),
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project = _pytest_project(Path(td))
        state = server.State(Path(td) / "state")
        event = _run(_runner(state, writer), state, project)

        assert len(writer.calls) == 2
        assert event["stop_reason"] == "stopped"
        assert "[Completion blocked:" not in event["summary"]


def test_blocked_note_vocabulary_is_closed() -> None:
    expected = {
        "unobserved",
        "max_repair_rounds",
        "turn_budget_exhausted",
        "environment_failure",
        "provider_failure",
        "repair_context_unavailable",
        "repair_not_admitted",
    }
    assert set(COMPLETION_BLOCKED_NOTES) == expected
    for reason in sorted(expected):
        result = _blocked_result(RunResult("claimed done", "done", 1), reason)
        assert result.stop_reason == "blocked"
        assert "[Completion blocked:" in result.summary
