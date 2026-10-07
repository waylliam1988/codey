"""Terminal generation cannot introduce declarations absent from the live run."""
import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult


def test_terminal_rejects_new_tool_declaration_without_sending(monkeypatch):
    provider = ApiProvider("http://fixture.test/v1", "fixture", tool_choice="auto")
    read = ProviderToolDefinition("read", "read", {"type": "object"})
    write = ProviderToolDefinition("write", "write", {"type": "object"})
    monkeypatch.setattr(provider, "_post_chat", lambda *a, **k: {"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"id": "read1", "function": {"name": "read", "arguments": "{}"}}]}}]})
    provider.send_turn("fixture", [read])
    sends = []
    monkeypatch.setattr(provider, "send_tool_results", lambda *a, **k: sends.append(a))
    with pytest.raises(ValueError, match="declared"):
        provider.acknowledge_tool_results([ProviderToolResult("read1", "OK")], [write])
    assert not sends
