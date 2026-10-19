"""The actual task entry survives process death after a tool settlement."""

import json

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_recovery import run_recovery_case


def test_real_process_restart_delivers_settled_tool_without_reexecution(tmp_path):
    target = GateTarget("http://scripted.invalid/v1", "scripted", 32768, 8192, 12000,
                        request_timeout=90)
    (tmp_path / "input.json").write_text(json.dumps({
        "project": str(tmp_path / "project"), "state": str(tmp_path / "state"),
    }), encoding="utf-8")
    result = run_recovery_case(target, tmp_path, scripted=True)
    if not result["ok"]:
        logs = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*-worker.log"))
        raise AssertionError(f"{result}\n{logs}")
    assert result["edit_executions"] == 1
    assert result["original_result_delivered"]
    assert result["policy_preserved"]
    assert not list((tmp_path / "state" / "runtime" / "sessions").glob("*.jsonl"))
