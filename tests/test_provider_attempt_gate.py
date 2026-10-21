"""The provider attempt gate runs once per physical attempt, immediately before transport."""
from __future__ import annotations

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.error_classification import RequestPrepError
from codey.providers.token_accounting import RequestContextCount


def _provider() -> ApiProvider:
    return ApiProvider(
        "http://localhost:9/v1", "fixture",
        request_counter=lambda _payload, *, deadline: RequestContextCount(1, "estimated"),
    )


def test_attempt_gate_rejects_before_any_transport(monkeypatch):
    provider = _provider()
    sent = []

    def generate(*_args, **_kwargs):
        sent.append(True)
        raise AssertionError("transport must not run after gate rejection")

    monkeypatch.setattr(api_transport, "generate", generate)
    def reject(_client, _payload):
        raise ValueError("revoked Ghost")

    provider.bind_attempt_gate(reject)
    with pytest.raises(RequestPrepError, match="payload admission failed: revoked Ghost"):
        provider.send("hello")
    assert sent == []


def test_attempt_gate_rechecks_each_retry(monkeypatch):
    provider = _provider()
    gates = []
    calls = []

    def gate(_client, payload):
        gates.append(payload)

    def generate(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            error = api_transport.GenerationNotSentError("pre-submission EOF")
            error.reason_code = "pre_submission_tls_eof"
            raise error
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_attempt_gate(gate)
    assert provider.send("hello") == "ok"
    assert len(calls) == 2
    assert len(gates) == 2
