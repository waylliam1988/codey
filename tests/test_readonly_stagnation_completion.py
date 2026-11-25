"""Deterministic contract for a read-only verified task that stalls."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from codey.agents.request import AgentRequest
from codey.types.json_value import decode_json_object
from tests.manual.readonly_stagnation_replay import replay


class ReplayOptions(TypedDict, total=False):
    verification_passes: bool
    verification_command: str
    max_turns: int
    project_changes_required: bool
    mutate_after_check: bool
    cancel_after_initial: bool


def test_readonly_completion_from_repair_settled_preserves_phase_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.completion.verification_policy import VerificationCandidate
    from codey.durable import DurableSession
    from codey.operations import project_candidate_validation as validation
    from codey.runtime.core.operation_payload import LEAF_REPAIR_SETTLED
    from codey.runtime.core.run_result import RunResult
    from codey.runtime.observe.execution_evidence import CheckEvidence
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from tests.support.sqlite_store import TemporarySQLiteStore
    project = tmp_path / "project"
    project.mkdir()
    check = VerificationCandidate("python -m unittest discover", ".", "fixture")
    durable = DurableSession(TemporarySQLiteStore())
    durable.admit_input(
        conversation_id="s1", request_id="request-run-1", task_id="run-1",
        text="readonly stagnation", provider_snapshot={"provider_id": "deepseek"},
    )
    durable.start_task("run-1", state={})
    mutation_line = RuntimeMutationLine(durable=durable)
    mutation_line.accept_operation(
        session_id="s1", run_id="run-1", project=str(project), provider_id="deepseek",
        turn_budget=12, max_repair_rounds=1, task_kind="project",
    )
    mutation_line.mark_writer_running("s1", "run-1", provider_id="deepseek")
    mutation_line.mark_writer_settled(
        "s1", "run-1", provider_id="deepseek", turns_used=5, stop_reason="no_progress",
    )
    mutation_line.record_completion_proof(
        "s1", "run-1", proof_ref="completion_proof:0123456789abcdef", proof_status="failed",
    )
    mutation_line.admit_repair_context("s1", "run-1", context_ref="sha256:" + "a" * 64)
    mutation_line.mark_repair_running("s1", "run-1", provider_id="deepseek")
    operation = mutation_line.mark_repair_settled(
        "s1", "run-1", provider_id="deepseek", turns_used=7, stop_reason="no_progress",
    )
    assert operation is not None and operation.leaf == LEAF_REPAIR_SETTLED
    from codey.agents.writer_failover import CheckpointView, WriterFailoverRunner
    from codey.app.context import AppContext
    from codey.operations.context import RunHooks, RunWork
    from codey.operations.project_completion_context import (
        AgentAccess,
        ProjectCompletionDeps,
        ProjectRun,
        ReviewAccess,
        RuntimeAccess,
        VerificationAccess,
    )
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.diagnostics import ProviderFailure, capture_provider_failure
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.task.model import TaskSubmission
    from tests.manual.readonly_stagnation_replay import RepeatingReadProvider
    from tests.support.task_entry_helpers import entry_frame

    state = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    provider = RepeatingReadProvider()
    current = state.workspace_revisions.current_state(project)
    evidence = ExecutionEvidence(workspace_revision=current.revision, workspace_fingerprint=current.fingerprint)
    evidence.observe_check(CheckEvidence(check.command, check.cwd, exit_code=0,
        workspace_revision=current.revision, workspace_fingerprint=current.fingerprint), succeeded=True)
    request = TaskSubmission("s1", str(project), "Verify without edits", 12, False, "deepseek", run_id="run-1")
    frame = entry_frame(request)
    frame.entry_policy = TaskPolicy(frozenset({"project.read", "project.verify"}))

    def capture_failure(*, model: str, action: str, page: object, error: BaseException) -> ProviderFailure:
        return capture_provider_failure(model=model, action=action, page=page, error=error)

    def collect_changes(*args: object, **kwargs: object) -> dict[str, object]:
        return {"ok": True, "changed_count": 0, "files": []}

    def covers(*args: object, **kwargs: object) -> bool:
        return True

    def submit(*args: object, **kwargs: object) -> RunResult:
        return RunResult("submitted", stop_reason="done", turns=2)

    def unused_writer(request: AgentRequest) -> RunResult:
        raise AssertionError("the writer attempt is replaced in this unit test")

    def unused_review(**kwargs: object) -> None:
        raise AssertionError("the unit test does not request review")

    deps = ProjectCompletionDeps(
        state=state, agent=AgentAccess(run=unused_writer,
            capture_provider_failure=capture_failure),
        verification=VerificationAccess(collect_changes=collect_changes, workspace_revisions=state.workspace_revisions),
        review=ReviewAccess(run=unused_review), runtime=RuntimeAccess(mutations=mutation_line),
    )
    failover = WriterFailoverRunner(
        provider, "deepseek", 0, set(),
        attempt=lambda spec, note: RunResult("unused"), select_next=lambda tried: None,
        connect=lambda selected: provider, close=lambda active: None,
        needs_canary=lambda selected: False, run_canary=lambda selected, active: True,
        capture_failure=lambda model, action, error: capture_provider_failure(
            model=model, action=action, page=None, error=error),
        record_failure=lambda model, failure: None, record_success=lambda model: None,
        clear_session=lambda model: None, on_switch=lambda model: None,
        refresh_checkpoint=lambda: CheckpointView(), stopped=lambda: False,
    )
    hooks = RunHooks(lambda event: None, lambda request: None, lambda update: None,
        lambda model, failure: None, lambda update: None, lambda: (), None)
    ctx = ProjectRun(deps, frame, RunWork([], evidence, operation=operation), hooks,
        str(project), state, request=request, failover=failover, verification_candidates=(check,),
        result=RunResult("stalled", stop_reason="no_progress", turns=7),
        task_changes=collect_changes(), writer_attempt_index=1)
    monkeypatch.setattr(validation, "check_covers_selected_candidate", covers)
    monkeypatch.setattr(validation, "_run_one_writer_attempt", submit)
    try:
        validation.submit_readonly_completion_candidate(ctx)
        settled = ctx.work.operation
        assert settled is not None
        assert settled.leaf == LEAF_REPAIR_SETTLED
        assert settled.turns_used == 9
        assert settled.candidate_validation_attempted is True
        assert settled.repair_rounds == 1
    finally:
        state.close()



def test_fresh_readonly_verification_gets_one_bounded_completion_submission(
    tmp_path: Path,
) -> None:
    report = replay(tmp_path / "positive", submit_on_recovery=True)

    # The task has a fresh, successful registered check and the workspace is
    # unchanged. Repeated reads must not trigger another test run, but the
    # runtime should give the agent one bounded read-only done submission.
    assert report["successful_verification_runs"] == 1
    assert report["files_unchanged"] is True
    assert report["recovery_prompt_seen"] is True
    assert report["provider_calls"] == 6
    assert report["recovery_attempted"] is True
    terminal = report["terminal"]
    assert isinstance(terminal, dict)
    assert terminal["stop_reason"] == "done"
    assert report["proof"] == "complete"
    assert report["permission_profiles"] == ["coding_writer", "planning_readonly"]
    assert report["conversation_modes"] == ["project", "project"]
    assert report["proof_refs"]


def test_readonly_completion_rejects_rerunning_the_already_passed_check(
    tmp_path: Path,
) -> None:
    report = replay(
        tmp_path / "repeat-check",
        submit_on_recovery=True,
        repeat_check_on_recovery=True,
    )

    assert report["successful_verification_runs"] == 1
    assert report["verification_run_attempts"] == 1
    assert report["physical_verification_executions"] == 1
    assert report["denied_verification_requests"] == 1
    assert report["files_unchanged"] is True
    assert report["recovery_attempted"] is True
    terminal = report["terminal"]
    assert isinstance(terminal, dict)
    assert terminal["stop_reason"] == "done"
    assert report["proof"] == "complete"


def test_failed_authorized_verification_has_real_failure_and_no_readonly_recovery(
    tmp_path: Path,
) -> None:
    report = replay(tmp_path / "failed-check", submit_on_recovery=True, verification_passes=False)

    assert report["successful_verification_runs"] == 0
    assert report["verification_run_attempts"] == 1
    assert report["verification_exit_codes"] == [1]
    assert report["recovery_attempted"] is False
    assert report["proof"] in (None, "")
    assert decode_json_object(report["terminal"]).get("stop_reason") != "done"


def test_readonly_stagnation_still_stops_when_bounded_submission_is_not_made(
    tmp_path: Path,
) -> None:
    report = replay(tmp_path / "negative", submit_on_recovery=False)

    assert report["successful_verification_runs"] == 1
    assert report["files_unchanged"] is True
    assert report["recovery_attempted"] is True
    calls = report["provider_calls"]
    assert isinstance(calls, int) and calls <= 7
    terminal = report["terminal"]
    assert isinstance(terminal, dict)
    assert terminal["stop_reason"] in {"no_progress", "max_turns"}
    assert report["proof"] in (None, "")
    assert report["proof_refs"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verification_passes": False},
        {"verification_command": "python -m compileall -q ."},
        {"max_turns": 6},
        {"project_changes_required": True},
        {"mutate_after_check": True},
        {"cancel_after_initial": True},
    ],
)
def test_readonly_recovery_requires_current_green_unchanged_authorized_work(
    tmp_path: Path, kwargs: ReplayOptions
) -> None:
    report = replay(tmp_path / "boundary", submit_on_recovery=True, **kwargs)

    assert report["recovery_attempted"] is False
    assert report["proof"] in (None, "")
    assert decode_json_object(report["terminal"]).get("stop_reason") != "done"
