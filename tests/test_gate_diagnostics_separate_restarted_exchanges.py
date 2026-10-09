"""New provider instances must not merge exchange 1 across process recovery."""

import json
from unittest.mock import patch

from tests.test_api_generation_observations_no_replay import Response
from tools.local_model_gate_attempts import GateTarget, RecordingProvider, _provider_metrics


def test_active_responses_remain_distinct_after_restart_without_guessing_terminal_status(tmp_path):
    target = GateTarget("http://localhost:5001/v1", "fake", 32768, 8192, 12000)
    active = {"choices": [{"finish_reason": "stop"}]}
    terminal = {"choices": [{"finish_reason": "length"}]}
    with patch("codey.providers.api_transport.open_request", return_value=Response(json.dumps(active).encode())):
        RecordingProvider(target, tmp_path)._generate([{"role": "user", "content": "task"}])
    with patch("codey.providers.api_transport.open_request", return_value=Response(json.dumps(terminal).encode())):
        RecordingProvider(target, tmp_path)._generate([{"role": "tool", "tool_call_id": "c1", "content": "OK"}], [])
    metrics = _provider_metrics(tmp_path)
    assert metrics["logical_sends"] == 2
    assert metrics["http_attempts"] == 2
    assert metrics["active_finish_reasons"] == ["stop", "length"]
    assert metrics["terminal_finish_reasons"] == []
