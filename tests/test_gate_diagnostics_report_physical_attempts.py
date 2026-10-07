"""Diagnostics count an unknown generation once, without automatic replay."""
import json
from unittest.mock import patch

import pytest

from codey.providers.api_transport import GenerationUnknownError
from tests.test_api_generation_observations_no_replay import Response
from tools.local_model_gate_attempts import GateTarget, RecordingProvider, _provider_metrics


def test_real_recorder_counts_unknown_wire_attempt_without_retry(tmp_path):
    provider = RecordingProvider(GateTarget("http://localhost:5001/v1", "fake", 32768, 8192, 12000), tmp_path)
    with patch("codey.providers.api_transport.open_request", side_effect=[Response(b""), Response(b'{"choices":[]}')]) as send, pytest.raises(GenerationUnknownError):
        provider._post_chat([])
    assert send.call_count == 1
    rows = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text().splitlines()]
    assert len([row for row in rows if row["type"] == "request"]) == 1
    wire = [row for row in rows if row["type"] == "wire_attempt"]
    assert [(row["attempt"], row["phase"]) for row in wire] == [(1, "request"), (1, "unknown")]
    assert len({row["request_sha256"] for row in wire}) == 1
    metrics = _provider_metrics(tmp_path)
    assert metrics["logical_sends"] == 1
    assert metrics["http_attempts"] == 1
    assert metrics["http_retries"] == 0
