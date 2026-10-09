"""A rejected native response cannot erase valid inputs or settled tool receipts."""
import copy

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationUnknownError
from codey.providers.base import ProviderToolDefinition, ProviderToolResult
from codey.providers.error_classification import OutputLengthError


def body(name, args, *, finish="tool_calls", identity="call-1"):
    return {"choices": [{"finish_reason": finish, "message": {"tool_calls": [
        {"id": identity, "type": "function", "function": {"name": name, "arguments": args}}]}}]}


@pytest.mark.parametrize("protocol,finish", [("openai-completions", "tool_calls"),
    ("openai-completions", "length"), ("openai-responses", "tool_calls")])
def test_invalid_followup_retains_previous_call_and_its_actual_result(monkeypatch, protocol, finish):
    provider = ApiProvider("http://fixture.test/v1", "fixture", api_protocol=protocol)
    tools = [ProviderToolDefinition("read_file", "Read file", {"type": "object"})]
    bodies = [body("read_file", '{"path":"app.py"}'), body("edit", '{"path":', finish=finish)]
    if protocol == "openai-responses":
        bodies = [{"status": "completed", "output": [{"type": "function_call", "call_id": "call-1",
            "name": name, "arguments": args}]} for name, args in [("read_file", '{"path":"app.py"}'), ("edit", '{"path":')]]
    replies = iter(bodies)
    monkeypatch.setattr(provider, "_generate", lambda *a, **k: next(replies))
    provider.send_turn("Inspect before editing", tools)
    prefix = copy.deepcopy(provider.context_ledger.view)
    results = [ProviderToolResult("call-1", "actual current file")]
    if finish == "length" or protocol == "openai-responses":
        with pytest.raises(OutputLengthError if finish == "length" else RuntimeError):
            provider.send_tool_results(results, tools)
    else:
        turn = provider.send_tool_results(results, tools)
        assert not turn.tool_calls
    assert provider.context_ledger.view[:len(prefix)] == prefix
    key = "content" if protocol == "openai-completions" else "output"
    assert provider.context_ledger.events()[-1][key] == "actual current file"
    assert provider._messages[-1][key] == "actual current file"
    provider._codec.validate_view(provider._messages)


def test_initial_user_request_survives_malformed_reply_without_retaining_bad_call(monkeypatch):
    provider = ApiProvider("http://fixture.test/v1", "fixture")
    monkeypatch.setattr(provider, "_generate", lambda *a, **k: body("edit", '{"path":'))
    turn = provider.send_turn("Keep the user's exact request")
    assert not turn.tool_calls
    assert provider.context_ledger.events() == [{"role": "user", "content": "Keep the user's exact request"}]
    assert not any(item.get("tool_calls") for item in provider.context_ledger.view)


def test_cancelled_decode_failure_cannot_restore_late_inputs(monkeypatch):
    provider = ApiProvider("http://fixture.test/v1", "fixture")
    def cancelled_response(*a, **k):
        provider.abandon_inflight()
        return body("edit", '{"path":')
    monkeypatch.setattr(provider, "_generate", cancelled_response)
    with pytest.raises(GenerationUnknownError):
        provider.send_turn("late request")
    assert provider._messages == [] and provider.context_ledger.view == []
