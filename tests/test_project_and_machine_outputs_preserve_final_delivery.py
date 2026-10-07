"""Project and machine projections retain proven completion and delivery facts."""
from types import SimpleNamespace
from unittest.mock import patch

from codey.app.event_payloads import machine_event_payload
from codey.operations.project_completion_flow import _build_project_done_event
from codey.runtime.core.run_result import RunResult


def test_project_terminal_preserves_final_delivery_and_proof():
    proof = SimpleNamespace(to_payload=lambda: {"proof_id": "completion_proof:0123456789abcdef", "status": "complete"})
    result = RunResult("done locally", "delivery_pending", 2, proof=proof)
    result.delivery = "unknown"
    ctx = SimpleNamespace(result=result, task_changed=False, task_changes=None, research_result=None,
                          receipt=SimpleNamespace(to_dict=lambda: {}), frame=SimpleNamespace(run_id="r", provider_id="zen"),
                          request=SimpleNamespace(session_id="s", max_turns=4), work=None)
    with patch("codey.operations.project_completion_flow.task_done_event", side_effect=lambda **k: k):
        event = _build_project_done_event(ctx)
    assert event["final_delivery"] == "unknown"
    assert event["receipt"]["completion_proof"] == proof.to_payload()


def test_machine_terminal_keeps_explicit_final_delivery():
    event = machine_event_payload({"type": "task_done", "run_id": "r", "session_id": "s",
                                  "stop_reason": "delivery_pending", "final_delivery": "unknown"})
    assert event["final_delivery"] == "unknown"
