"""Attempt accounting remains truthful on timeout, exception and repetition."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

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
    def headless(request, *, emit_jsonl, connect_provider):
        connected.append(connect_provider("local"))
        (request.project / "failure.txt").write_text("diagnose me", encoding="utf-8")
        raise RuntimeError("deliberate failure")
    provider = object()
    with (
        mock.patch.object(gate, "run_headless", side_effect=headless),
        mock.patch.object(runner, "make_provider", return_value=provider) as factory,
    ):
        result = gate.run_agent_case("edit", target=target, case_dir=tmp_path)
    assert result["ok"] is False
    assert connected == [provider]
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
    def headless(request, *, emit_jsonl, connect_provider):
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
