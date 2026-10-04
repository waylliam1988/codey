"""Attempt accounting remains truthful on timeout, exception and repetition."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from tools import local_model_gate_attempts as runner
from tools import local_model_release_gate as gate


def test_two_runs_never_overwrite_first(tmp_path):
    first = runner.create_run_dir(tmp_path)
    (first / "summary.json").write_text("first", encoding="utf-8")
    second = runner.create_run_dir(tmp_path)
    assert first != second
    assert (first / "summary.json").read_text(encoding="utf-8") == "first"


def test_timeout_still_has_failed_result_and_artifacts(tmp_path):
    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    def expire(*args, **kwargs):
        config = json.loads((tmp_path / "edit-1/input.json").read_text())
        (Path(config["project"]) / "pricing.py").write_text("partial edit", encoding="utf-8")
        raise subprocess.TimeoutExpired("worker", 1)
    with (
        mock.patch.object(runner, "start_process", return_value=(object(), object())),
        mock.patch.object(runner, "wait_process", side_effect=expire),
    ):
        result = runner.run_case_process("edit", tmp_path / "edit-1", target, timeout=1)
    assert result["ok"] is False
    assert result["failure_stage"] == "case_timeout"
    assert result["model"] == "test-model"
    assert (tmp_path / "edit-1/result.json").is_file()
    assert (tmp_path / "edit-1/project/pricing.py").is_file()


def test_child_exception_does_not_drop_next_attempt(tmp_path):
    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    def attempt(case, case_dir, target, *, timeout):
        if case == "chat":
            raise OSError("server unavailable")
        return {"case": case, "ok": True, "scope": "objective_task"}
    with mock.patch.object(runner, "run_case_process", side_effect=attempt):
        results = runner.run_attempts(("chat", "edit"), tmp_path, target, repeat=2, timeout=1)
    assert len(results) == 4
    assert [item["ok"] for item in results] == [False, True, False, True]
    assert json.loads((tmp_path / "summary.json").read_text())["attempts"] == 4


def test_control_plane_and_answer_quality_are_not_objective_successes():
    results = [
        {"case": "ghost", "ok": True, "scope": "control_plane"},
        {"case": "planning", "ok": True, "scope": "conversation_safety"},
        {"case": "edit", "ok": False, "scope": "objective_task"},
    ]
    summary = runner.summarize(results)
    assert summary["objective_tasks"] == {"attempts": 1, "passed": 0, "artifacts_correct": 0, "artifacts_observed": 0}
    assert summary["answer_quality"] == "not_evaluated"


def test_correct_artifact_is_counted_separately_from_terminal_completion():
    summary = runner.summarize([
        {"case": "edit", "ok": False, "scope": "objective_task", "work_correct": True},
    ])
    assert summary["objective_tasks"]["passed"] == 0
    assert summary["objective_tasks"]["artifacts_correct"] == 1


def test_summary_preserves_task_failure_matrix_and_root_cause_class():
    summary = runner.summarize([
        {
            "case": "edit",
            "ok": False,
            "scope": "objective_task",
            "task_kind": "bug_fix",
            "failure_kind": "truncation",
            "root_cause_class": "provider_boundary",
            "root_cause_evidence": {"provider": "provider.jsonl"},
        },
        {
            "case": "edit",
            "ok": True,
            "scope": "objective_task",
            "task_kind": "bug_fix",
            "failure_kind": "none",
            "root_cause_class": "none",
        },
    ])

    assert summary["matrix"] == {
        "bug_fix": {
            "truncation": {"attempts": 1, "passed": 0},
            "none": {"attempts": 1, "passed": 1},
        },
    }
    assert summary["root_causes"] == {"provider_boundary": 1}


def test_gate_result_classification_keeps_observation_separate_from_root_cause():
    assert runner.case_task_kind("edit") == "bug_fix"
    assert runner.case_task_kind("create") == "feature"
    assert runner.case_task_kind("references") == "refactor"
    assert runner.case_task_kind("hybrid") == "browser"

    truncated = runner.annotate_result({
        "case": "edit",
        "ok": False,
        "failure_stage": "completion:provider_failure",
        "provider_metrics": {"active_finish_reasons": ["length"]},
    })
    assert truncated["failure_kind"] == "truncation"
    assert truncated["root_cause_class"] == "undetermined"

    timed_out = runner.annotate_result({
        "case": "edit",
        "ok": False,
        "failure_stage": "case_timeout",
    })
    assert timed_out["failure_kind"] == "timeout"
    assert timed_out["root_cause_class"] == "undetermined"

    passed = runner.annotate_result({"case": "edit", "ok": True})
    assert passed["failure_kind"] == "none"
    assert passed["root_cause_class"] == "none"


def test_matrix_labels_are_restricted_and_keep_evidence_source():
    result = runner.annotate_result({
        "case": "hybrid",
        "ok": False,
        "failure_stage": "tool_order",
        "failure_evidence": {"events": "events.jsonl", "provider": "provider.jsonl"},
    })
    assert result["task_kind"] == "browser"
    assert result["failure_kind"] == "tool_error"
    assert result["root_cause_class"] == "undetermined"
    assert result["failure_evidence"] == {"events": "events.jsonl", "provider": "provider.jsonl"}

    with pytest.raises(ValueError, match="failure_kind"):
        runner.summarize([{
            "case": "edit", "ok": False, "task_kind": "bug_fix",
            "failure_kind": "made_up_failure", "root_cause_class": "undetermined",
        }])


def test_task_axis_covers_release_matrix_vocabulary():
    assert runner.case_task_kind("tests") == "tests"
    assert runner.case_task_kind("research") == "research"
    assert runner.case_task_kind("recovery") == "recovery"


def test_tests_is_objective_and_recovery_is_restart_safety():
    assert runner.case_scope("tests") == "objective_task"
    assert runner.case_scope("recovery") == "recovery_safety"


def test_failure_axis_detects_restart_duplicate_and_resume_evidence():
    assert runner.annotate_result({
        "case": "recovery", "ok": False, "failure_stage": "provider_restart",
    })["failure_kind"] == "provider_restart"
    assert runner.annotate_result({
        "case": "edit", "ok": False, "failure_stage": "duplicate_event",
    })["failure_kind"] == "duplicate_event"
    assert runner.annotate_result({
        "case": "recovery", "ok": False, "failure_stage": "resume_failed",
    })["failure_kind"] == "resume"


def test_root_cause_requires_explicit_evidence_reference():
    with pytest.raises(ValueError, match="root_cause_evidence"):
        runner.summarize([{
            "case": "edit", "ok": False, "task_kind": "bug_fix",
            "failure_kind": "tool_error", "root_cause_class": "production_defect",
        }])


def test_summary_can_require_a_complete_matrix_without_marking_missing_cells_passed():
    summary = runner.summarize([
        {"case": "edit", "attempt": 1, "ok": True, "scope": "objective_task"},
    ], expected_cases=("edit", "create"), repeat=1)
    assert summary["matrix_complete"] is False
    assert summary["missing_attempts"] == [{"case": "create", "attempt": 1}]
    assert summary["ok"] is False


def test_unconfigured_research_is_environment_with_evidence():
    result = runner.annotate_result({
        "case": "research", "ok": False,
        "failure_stage": "research_environment_unavailable",
        "error": "ERROR: Research is not configured",
        "root_cause_evidence": {"events": "events.jsonl"},
    })
    assert result["failure_kind"] == "tool_error"
    assert result["root_cause_class"] == "environment"


def test_environment_result_gets_evidence_paths_before_summary(tmp_path):
    result = runner.annotate_result({
        "case": "research", "ok": False,
        "failure_stage": "research_environment_unavailable",
    })
    result["root_cause_evidence"] = {"events": "events.jsonl", "provider": "provider.jsonl"}
    summary = runner.summarize([result])
    assert summary["root_causes"] == {"environment": 1}


def test_timeout_does_not_become_artifact_error_on_partial_provider_record(tmp_path):
    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    def expire(*args, **kwargs):
        (tmp_path / "edit/provider.jsonl").write_text('{"type":"response"', encoding="utf-8")
        raise subprocess.TimeoutExpired("worker", 1)
    with (
        mock.patch.object(runner, "start_process", return_value=(object(), object())),
        mock.patch.object(runner, "wait_process", side_effect=expire),
    ):
        result = runner.run_case_process("edit", tmp_path / "edit", target, timeout=1)
    assert result["failure_stage"] == "case_timeout"
    assert result["provider_metrics"]["capture_incomplete"] is True


def test_agent_uses_pinned_provider_and_preserves_failed_project(tmp_path):
    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    state.mkdir()
    (tmp_path / "input.json").write_text(json.dumps({"project": str(project), "state": str(state)}), encoding="utf-8")
    connected = []
    def headless(request, *, emit_jsonl, connect_provider, connect_reviewer):
        connected.append(connect_provider("local"))
        connected.append(connect_reviewer("local"))
        (request.project / "failure.txt").write_text("diagnose me", encoding="utf-8")
        raise RuntimeError("deliberate failure")
    provider = object()
    with (
        mock.patch.object(gate, "run_headless", side_effect=headless),
        mock.patch.object(runner, "make_provider", return_value=provider) as factory,
    ):
        result = gate.run_agent_case("edit", target=target, case_dir=tmp_path)
    assert result["ok"] is False
    assert connected == [provider, provider]
    assert factory.call_args.args[0] == target
    assert (Path(result["project"]) / "failure.txt").read_text() == "diagnose me"
    assert (tmp_path / "events.jsonl").is_file()


def test_correct_files_and_done_cannot_bypass_hybrid_tool_evidence(tmp_path):
    from codey.app.headless_runner import HeadlessResult

    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    state.mkdir()
    (tmp_path / "input.json").write_text(json.dumps({"project": str(project), "state": str(state)}), encoding="utf-8")
    def headless(request, *, emit_jsonl, connect_provider, connect_reviewer):
        (project / "pricing.py").write_text(
            "def discounted_price(price, percent):\n    return price * (1 - percent / 100)\n", encoding="utf-8",
        )
        emit_jsonl({"type": "task_start", "run_id": "r", "session_id": "s"})
        emit_jsonl({"type": "task_done", "run_id": "r", "session_id": "s", "stop_reason": "done"})
        return HeadlessResult(0, "r", "s", "done")
    with mock.patch.object(gate, "run_headless", side_effect=headless):
        result = gate.run_agent_case("hybrid", target=target, case_dir=tmp_path)
    assert result["verification"]["ok"] is True
    assert result["single_session"]["ok"] is True
    assert result["ok"] is False
    assert result["failure_stage"] == "tool_order"


def test_watchdog_really_terminates_worker_and_keeps_partial_project(tmp_path):
    target = runner.GateTarget("http://model.test/v1", "test-model", 32768, 8192, 12000)
    processes = []
    def start_worker(command, *, cwd):
        config = json.loads((tmp_path / "edit/input.json").read_text())
        source = (
            "import pathlib,time,sys; "
            "pathlib.Path(sys.argv[1]).write_text('partial edit'); time.sleep(60)"
        )
        proc, job = runner.start_process_original(
            [sys.executable, "-c", source, str(Path(config["project"]) / "pricing.py")], cwd=cwd,
        )
        processes.append(proc)
        return proc, job
    with (
        mock.patch.object(runner, "start_process_original", runner.start_process, create=True),
        mock.patch.object(runner, "start_process", side_effect=start_worker),
    ):
        result = runner.run_case_process("edit", tmp_path / "edit", target, timeout=2)
    assert result["failure_stage"] == "case_timeout"
    assert processes[0].poll() is not None
    assert (tmp_path / "edit/project/pricing.py").read_text() == "partial edit"
