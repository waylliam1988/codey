"""Terminal tool results close local protocol history without generating a reply."""
import copy

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.error_classification import RequestPrepError


@pytest.fixture(params=["openai-completions", "openai-responses"])
def live_provider(request, monkeypatch):
    provider = ApiProvider("http://fixture.test/v1", "fixture", api_protocol=request.param)
    tool = ProviderToolDefinition("read", "Read a file", {"type": "object"})
    body = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}}]}
    if request.param == "openai-responses":
        body = {"status": "completed", "output": [
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": "{}"}]}
    monkeypatch.setattr(provider, "_generate", lambda *args, **kwargs: copy.deepcopy(body))
    provider.send_turn("Read current file", [tool])
    return provider, [tool]


def test_terminal_receipt_commits_exactly_once_without_a_physical_generation(live_provider, monkeypatch):
    provider, tools = live_provider
    before = len(provider.context_ledger.events())
    sends = []
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "Acknowledged"}}]}
    if provider.api_protocol == "openai-responses":
        body = {"status": "completed", "output": []}
    monkeypatch.setattr(provider, "_generate", lambda *args, **kwargs: sends.append(args) or body)
    turn = provider.acknowledge_tool_results([ProviderToolResult("call-1", "Stopped by user")], tools)
    assert sends == []
    assert turn.text == "" and not turn.tool_calls
    assert len(provider.context_ledger.events()) == before + 1
    assert provider.context_ledger.events()[-1] in provider.context_ledger.view
    assert "Stopped by user" in str(provider.context_ledger.view[-1])
    provider._codec.validate_view(provider.context_ledger.view)


@pytest.mark.parametrize("identity", ["not-declared", "call-1"])
def test_unpaired_or_duplicate_terminal_results_cannot_mutate_history(live_provider, identity):
    provider, tools = live_provider
    results = [ProviderToolResult(identity, "Not executed")]
    if identity == "call-1":
        results *= 2
    view = copy.deepcopy(provider.context_ledger.view)
    events = provider.context_ledger.events()
    with pytest.raises(RequestPrepError):
        provider.acknowledge_tool_results(results, tools)
    assert provider.context_ledger.view == view
    assert provider.context_ledger.events() == events


def test_cancelled_history_is_not_revived_by_late_terminal_result(live_provider):
    provider, tools = live_provider
    provider.abandon_inflight()
    with pytest.raises((RequestPrepError, ValueError)):
        provider.acknowledge_tool_results([ProviderToolResult("call-1", "late")], tools)
    assert provider.context_ledger.view == []
