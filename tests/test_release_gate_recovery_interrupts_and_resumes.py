"""The recovery gate must restart real task execution, not repeat Ghost storage."""

import json

import pytest

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_release_gate import _worker


def test_recovery_worker_never_dispatches_to_ghost(tmp_path, monkeypatch):
    from dataclasses import asdict

    monkeypatch.setenv("NATIVE_TOOLS", "1")

    from tools import local_model_release_gate as gate

    target = GateTarget("http://localhost:5001/v1", "scripted", 32768, 8192, 12000, protocol="json")
    (tmp_path / "input.json").write_text(json.dumps({"target": asdict(target), "project": str(tmp_path / "project"),
                                                    "state": str(tmp_path / "state")}), encoding="utf-8")
    monkeypatch.setattr(gate, "run_ghost_case", lambda *a: pytest.fail("recovery is not a Ghost roundtrip"))
    monkeypatch.setattr(gate, "run_recovery_case", lambda target, directory: {"case": "recovery", "ok": True},
                        raising=False)
    assert _worker("recovery", tmp_path) == 0
    assert json.loads((tmp_path / "worker-result.json").read_text())["ok"] is True


@pytest.mark.parametrize("protocol", ["json", "native"])
def test_recovery_gate_restarts_and_finishes_without_repeating_edit(tmp_path, protocol):
    from tools.local_model_gate_recovery import run_recovery_case

    target = GateTarget("http://localhost:5001/v1", "scripted", 32768, 8192, 12000, protocol=protocol)
    config = {"project": str(tmp_path / "project"), "state": str(tmp_path / "state")}
    (tmp_path / "input.json").write_text(json.dumps(config), encoding="utf-8")
    result = run_recovery_case(target, tmp_path, scripted=True)
    assert result["ok"] is True, result
    assert result["prepare_exit_code"] == 75
    assert result["prepare_pid"] != result["resume_pid"]
    assert result["edit_executions"] == 1
    assert result["original_result_delivered"] is True
    assert result["policy_preserved"] is True
    assert result["stop_reason"] == "done"
    assert result["verification"]["ok"] is True
    if protocol == "native":
        assert result["original_call_id_preserved"] is True
        assert result["delivery_mode"] == "fresh_window_fact_handoff"


def test_recovery_is_reported_as_restart_safety_not_storage_control_plane():
    from tools.local_model_gate_attempts import case_scope

    assert case_scope("recovery") == "recovery_safety"
    assert case_scope("ghost") == "control_plane"
