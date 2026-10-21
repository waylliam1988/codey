"""Attempt Gate lifecycle coverage on the production API provider boundary."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from codey.operations.kernel_transport import repair_native_dangling
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.error_classification import RequestPrepError

PROTOCOLS = ("openai-completions", "openai-responses")
READ = ProviderToolDefinition("read", "Read a file", {"type": "object"})


def _reply(protocol: str, *, text: str = "ok", call: ProviderToolCall | None = None) -> dict[str, object]:
    if protocol == "openai-completions":
        message: dict[str, object] = {"role": "assistant", "content": text}
        if call is not None:
            message["content"] = None
            message["tool_calls"] = [{
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }]
        return {"choices": [{"finish_reason": "tool_calls" if call else "stop", "message": message}]}
    output: list[dict[str, object]] = [{
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }]
    if call is not None:
        output.append({
            "type": "function_call",
            "id": "item-1",
            "call_id": call.id,
            "name": call.name,
            "arguments": json.dumps(call.arguments),
        })
    return {"status": "completed", "output": output}


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_gate_rechecks_native_result_after_revocation(monkeypatch: pytest.MonkeyPatch, protocol: str) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol)
    call = ProviderToolCall("call-1", "read", {"path": "a.txt"})
    replies = iter([_reply(protocol, call=call), _reply(protocol)])
    physical_payloads: list[dict[str, object]] = []
    gate_payloads: list[dict[str, object]] = []

    def generate(_endpoint: str, payload: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        physical_payloads.append(copy.deepcopy(payload))
        return next(replies)

    def gate(_client: ApiProvider, payload: dict[str, object]) -> None:
        gate_payloads.append(copy.deepcopy(payload))
        if len(gate_payloads) == 2:
            raise ValueError("revoked after first native turn")

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_attempt_gate(gate)
    try:
        assert provider.send_turn("read", [READ]).tool_calls == (call,)
        with pytest.raises(RequestPrepError, match="revoked after first native turn"):
            provider.send_tool_results([ProviderToolResult(call.id, "contents")], [READ])
        assert len(gate_payloads) == 2
        assert len(physical_payloads) == 1
        assert any(call.id in repr(payload) for payload in gate_payloads)
    finally:
        provider.close()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_native_error_receipt_uses_the_same_gate(monkeypatch: pytest.MonkeyPatch, protocol: str) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol)
    call = ProviderToolCall("call-2", "read", {"path": "b.txt"})
    physical_payloads: list[dict[str, object]] = []
    gate_calls = 0

    def generate(_endpoint: str, payload: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        physical_payloads.append(copy.deepcopy(payload))
        return _reply(protocol, call=call)

    def gate(_client: ApiProvider, _payload: dict[str, object]) -> None:
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 2:
            raise ValueError("receipt revoked")

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_attempt_gate(gate)
    try:
        first = provider.send_turn("read", [READ])
        assert first.tool_calls == (call,)
        with pytest.raises(RequestPrepError, match="receipt revoked"):
            repair_native_dangling(provider, first, True, [READ], "invalid native request")
        assert gate_calls == 2
        assert len(physical_payloads) == 1
    finally:
        provider.close()


def test_resumed_durable_context_is_admitted_at_the_next_send(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture")
    payloads: list[dict[str, object]] = []
    gate_payloads: list[dict[str, object]] = []

    def generate(_endpoint: str, payload: dict[str, object], *_args: object, **_kwargs: object) -> dict[str, object]:
        payloads.append(copy.deepcopy(payload))
        return _reply(provider.api_protocol)

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_context("resume-session")
    provider.load_durable_context(
        [{"role": "user", "content": "durable fact"}, {"role": "assistant", "content": "old answer"}],
        source_entry_ids=("entry-1", "entry-2"),
    )
    provider.bind_attempt_gate(lambda _client, payload: gate_payloads.append(copy.deepcopy(payload)))
    try:
        assert provider.send("continue from the saved checkpoint") == "ok"
        assert len(payloads) == len(gate_payloads) == 1
        encoded = repr(gate_payloads[0])
        assert "durable fact" in encoded and "continue from the saved checkpoint" in encoded
    finally:
        provider.close()


def test_compaction_auxiliary_inherits_attempt_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture")
    calls: list[tuple[str, dict[str, object]]] = []

    def generate(_endpoint: str, _payload: dict[str, object], *_args: object, **_kwargs: Any) -> dict[str, object]:
        return _reply("openai-completions")

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_attempt_gate(lambda client, payload: calls.append((client.purpose, copy.deepcopy(payload))))
    auxiliary = provider.fork_auxiliary()
    try:
        assert auxiliary.send("bounded work-state summary") == "ok"
        assert len(calls) == 1
        assert calls[0][0] == "compaction"
    finally:
        auxiliary.close()
        provider.close()


def test_terminal_receipt_does_not_claim_a_physical_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture")
    calls = []
    def generate(*_args: object, **_kwargs: Any) -> dict[str, object]:
        return _reply("openai-completions", call=ProviderToolCall("call-3", "read", {}))

    monkeypatch.setattr(api_transport, "generate", generate)
    provider.bind_attempt_gate(lambda _client, _payload: calls.append(True))
    try:
        provider.send_turn("read", [READ])
        before = len(calls)
        provider.acknowledge_tool_results([ProviderToolResult("call-3", "not executed")], [READ])
        assert len(calls) == before
    finally:
        provider.close()
