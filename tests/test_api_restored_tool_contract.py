"""Saved native contracts restore only over their own cold durable view."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from codey.providers.api_context import restore_declared_tools
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.requests import ProviderRequest
from codey.providers.transport import ProviderTransport
from codey.types.protocol_window import ProtocolWindowRef

PROTOCOLS = ("openai-completions", "openai-responses")
TOOLS = [ProviderToolDefinition("done", "Finish the task.", {"type": "object"})]


@contextmanager
def _cold(protocol: str, *, canonical: bool = True) -> Generator[ApiProvider, None, None]:
    client = ApiProvider("http://127.0.0.1:1/v1", "model", api_protocol=protocol, native_tools=True)
    try:
        client.bind_context("saved-response-session")
        messages: list[dict[str, object]] = [
            {"role": "user", "content": "original task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "saved-call", "type": "function", "function": {"name": "done", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "saved-call", "content": "Pending controller delivery."},
        ]
        client.load_durable_context(messages, source_entry_ids=("input", "saved-response", "pending") if canonical else ())
        yield client
    finally:
        client.close()


def _ref(client: ApiProvider) -> ProtocolWindowRef:
    ref = client.window_ref
    assert ref is not None
    return ref


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_cold_saved_native_call_can_close_locally_and_restore_idempotently(protocol: str) -> None:
    with _cold(protocol) as client:
        ref = _ref(client)
        before: object = client.context_ledger.view
        restore_declared_tools(client, ref, TOOLS)
        after: object = client.context_ledger.view
        assert after == before
        reply = client.acknowledge_tool_results_bound([ProviderToolResult("saved-call", "done")], ref, TOOLS)
        assert not reply.tool_calls
        closed: object = client.context_ledger.view
        restore_declared_tools(client, ref, TOOLS)
        repeated: object = client.context_ledger.view
        assert repeated == closed
        assert client._declared_tools == TOOLS
        assert client.window_ref == ref


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restore_rejects_another_window_without_setting_tools(protocol: str) -> None:
    with _cold(protocol) as client:
        ref = _ref(client)
        other = ProtocolWindowRef.new(ref.session_id, ref.binding)
        with pytest.raises(ValueError, match="window mismatch"):
            restore_declared_tools(client, other, TOOLS)
        assert client._declared_tools == []
        assert client.window_ref == ref


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restore_cannot_replace_an_existing_transport_contract(protocol: str) -> None:
    with _cold(protocol) as client:
        ref = _ref(client)
        restore_declared_tools(client, ref, TOOLS)
        different = [*TOOLS, ProviderToolDefinition("other", "Unrelated tool.", {"type": "object"})]
        with pytest.raises(ValueError, match="cannot replace a live"):
            restore_declared_tools(client, ref, different)
        assert client._declared_tools == TOOLS


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restore_rejects_history_without_durable_source_identities(protocol: str) -> None:
    with _cold(protocol, canonical=False) as client:
        with pytest.raises(ValueError, match="live protocol history"):
            restore_declared_tools(client, _ref(client), TOOLS)
        assert client._declared_tools == []


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restore_uses_the_real_provider_send_lock(protocol: str) -> None:
    with _cold(protocol) as client:
        with client._send_lock, pytest.raises(RuntimeError, match="provider busy"):
            restore_declared_tools(client, _ref(client), TOOLS)
        assert client._declared_tools == []
        restore_declared_tools(client, _ref(client), TOOLS)
        assert client._declared_tools == TOOLS


class WrappedProvider(ProviderTransport):
    def __init__(self, provider: ApiProvider) -> None:
        self.provider: object = provider

    def execute_request(self, request: ProviderRequest) -> object:
        raise AssertionError("contract restoration must not execute a request")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restore_uses_the_existing_api_owner_through_transport_wrappers(protocol: str) -> None:
    with _cold(protocol) as client:
        restore_declared_tools(WrappedProvider(client), _ref(client), TOOLS)
        assert client._declared_tools == TOOLS
