"""Manual gate diagnostics distinguish one logical send from HTTP retries."""
import json
from unittest.mock import patch

from tests.test_local_request_diagnostics_match_wire_attempts import Response
from tools.local_model_gate_attempts import GateTarget, RecordingProvider, _provider_metrics


def test_real_recorder_and_metrics_count_wire_attempts(tmp_path):
    provider = RecordingProvider(GateTarget("http://localhost:5001/v1", "fake", 32768, 8192, 12000), tmp_path)
    with patch("urllib.request.urlopen", side_effect=[Response(b""), Response(b'{"choices":[]}')]):
        assert provider._post_chat([]) == {"choices": []}
    rows = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text().splitlines()]
    assert len([row for row in rows if row["type"] == "request"]) == 1
    wire = [row for row in rows if row["type"] == "wire_attempt"]
    assert [(row["attempt"], row["phase"]) for row in wire] == [(1, "request"), (1, "retryable_error"), (2, "request"), (2, "response")]
    assert len({row["request_sha256"] for row in wire}) == 1
    metrics = _provider_metrics(tmp_path)
    assert metrics["logical_sends"] == 1
    assert metrics["http_attempts"] == 2
    assert metrics["http_retries"] == 1
