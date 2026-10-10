"""Deterministic contract for a read-only verified task that stalls."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from tests.manual.readonly_stagnation_replay import replay


def test_readonly_completion_from_repair_settled_preserves_phase_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.completion.verification_policy import VerificationCandidate
    from codey.operations import project_candidate_validation as validation
    from codey.runtime.core.operation_state import LEAF_REPAIR_SETTLED
    from codey.runtime.core.run_result import RunResult
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.observe.execution_evidence import CheckEvidence
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    project = tmp_path / "project"
    project.mkdir()
    check = VerificationCandidate("python -m unittest discover", ".", "fixture")
    mutation_line = RuntimeMutationLine(RuntimeSessionLog(tmp_path / "runtime"))
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
    evidence = SimpleNamespace(
        has_successful_checks=True,
        successful_checks=(CheckEvidence(check.command, check.cwd, exit_code=0),),
    )
    ctx = SimpleNamespace(
        result=RunResult("stalled", stop_reason="no_progress", turns=7),
        frame=SimpleNamespace(
            entry_policy=SimpleNamespace(allows=lambda _grant: True),
            provider_id="deepseek",
        ),
        work=SimpleNamespace(operation=operation, evidence=evidence),
        task_changed=False,
        task_changes={"ok": True, "changed_count": 0, "files": []},
        request=SimpleNamespace(max_turns=12, project_changes_required=False, session_id="s1"),
        state=SimpleNamespace(run_registry=SimpleNamespace(stop_flag=Event())),
        failover=SimpleNamespace(provider=object()),
        verification_candidates=(check,),
        project=project,
        writer_attempt_index=1,
        deps=SimpleNamespace(
            verification=SimpleNamespace(collect_changes=lambda *_a, **_k: {
                "ok": True, "changed_count": 0, "files": [],
            }),
            runtime=SimpleNamespace(mutations=mutation_line),
        ),
        tracker=object(),
        task_session=object(),
        hooks=SimpleNamespace(append_ledger=lambda _fn: None, update_checkpoint=lambda _fn: None),
    )
    monkeypatch.setattr(validation, "check_covers_selected_candidate", lambda *_a, **_k: True)
    monkeypatch.setattr(
        validation,
        "_run_one_writer_attempt",
        lambda *_a, **_k: RunResult("submitted", stop_reason="done", turns=2),
    )

    validation.submit_readonly_completion_candidate(ctx)

    assert ctx.work.operation.leaf == LEAF_REPAIR_SETTLED
    assert ctx.work.operation.turns_used == 9
    assert ctx.work.operation.candidate_validation_attempted is True
    assert ctx.work.operation.repair_rounds == 1


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
    assert (report["terminal"] or {}).get("stop_reason") != "done"


def test_readonly_stagnation_still_stops_when_bounded_submission_is_not_made(
    tmp_path: Path,
) -> None:
    report = replay(tmp_path / "negative", submit_on_recovery=False)

    assert report["successful_verification_runs"] == 1
    assert report["files_unchanged"] is True
    assert report["recovery_attempted"] is True
    assert report["provider_calls"] <= 7
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
    tmp_path: Path, kwargs: dict[str, object]
) -> None:
    report = replay(tmp_path / "boundary", submit_on_recovery=True, **kwargs)

    assert report["recovery_attempted"] is False
    assert report["proof"] in (None, "")
    assert (report["terminal"] or {}).get("stop_reason") != "done"
