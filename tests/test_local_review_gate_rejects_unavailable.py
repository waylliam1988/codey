"""The real smoke harness must reject unavailable/no-request review."""
import json
from types import SimpleNamespace

from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_review import run_review_case


def test_unavailable_review_is_not_a_passing_smoke(tmp_path, monkeypatch):
    (tmp_path / "input.json").write_text(json.dumps({
        "project": str(tmp_path / "project"), "state": str(tmp_path / "state"),
    }), encoding="utf-8")

    def unavailable(request, *, emit_jsonl, **kwargs):
        emit_jsonl({"type": "review", "text": "Review unavailable"})
        emit_jsonl({"type": "task_done", "stop_reason": "done"})
        return SimpleNamespace(exit_code=0, stop_reason="done", run_id="r", session_id="s")

    monkeypatch.setattr("codey.app.headless_runner.run_headless", unavailable)
    target = GateTarget("http://fixture/v1", "test", 32768, 8192, 12000)
    assert run_review_case(target, tmp_path)["ok"] is False
