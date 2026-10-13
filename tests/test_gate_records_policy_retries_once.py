"""Nested shell gates observe actual policy retries without double counting."""
import json

from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import LocalRequestPolicy, RetryDirective
from codey.providers.api_transport import GenerationRejectedError
from tools.local_model_gate_attempts import _provider_metrics, record_api_generations


def test_nested_recorder_counts_one_logical_send_and_three_physical_attempts(monkeypatch, tmp_path):
    calls = []
    def transport(_url, _payload, _headers, **kwargs):
        calls.append(kwargs["attempt_budget"].used)
        kwargs["observe"](attempt=1, data=b"{}", phase="request", response_bytes=0, seconds=0)
        if len(calls) < 3:
            raise GenerationRejectedError(429, '{"error":{"type":"server_error","message":"busy"}}',
                                          upstream_request_id=f"upstream-{len(calls)}")
        return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}

    class Policy(LocalRequestPolicy):
        def observe_failure(self, *_args):
            return RetryDirective(True)

    monkeypatch.setattr("codey.providers.api_transport.generate", transport)
    path = tmp_path / "provider.jsonl"
    provider = ApiProvider("http://fixture/v1", "fixture", request_policy=Policy())
    with record_api_generations(path), record_api_generations(path):
        assert provider.send("reply OK") == "OK"
    provider.close()
    assert calls == [1, 2, 3]
    metrics = _provider_metrics(tmp_path)
    assert (metrics["logical_sends"], metrics["http_attempts"], metrics["http_retries"]) == (1, 3, 2)
    errors = [json.loads(line) for line in path.read_text(encoding="utf8").splitlines() if '"type": "error"' in line]
    assert [row["upstream_request_id"] for row in errors] == ["upstream-1", "upstream-2"]
