"""Both API protocols obey the same atomic exchange and cancellation rules."""
import copy
import json
import threading

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.providers.error_classification import OutputLengthError

PROTOCOLS = ("openai-completions", "openai-responses")
READ = ProviderToolDefinition("read_file", "Read", {"type": "object"})


def reply_body(protocol, *, text="answer", reasoning="", call=None, limited=False):
    if protocol == "openai-completions":
        message = {"content": text, "reasoning_content": reasoning}
        if call:
            message["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments),
            }}]
        return {"choices": [{"finish_reason": "length" if limited else "stop", "message": message}]}
    output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
    if reasoning:
        output.insert(0, {"type": "reasoning", "summary": [{"text": reasoning}], "encrypted_content": "opaque"})
    if call:
        output.append({"type": "function_call", "id": "item-not-call", "call_id": call.id,
                       "name": call.name, "arguments": json.dumps(call.arguments)})
    return {"status": "incomplete" if limited else "completed", "output": output,
            "incomplete_details": {"reason": "max_output_tokens"} if limited else None}


@pytest.fixture(params=PROTOCOLS)
def provider(request):
    return ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=request.param)


@pytest.mark.parametrize("entry", ["send", "send_turn", "send_tool_results"])
def test_failed_exchange_keeps_committed_history_and_does_not_replay(provider, monkeypatch, entry):
    call = ProviderToolCall(" exact id ", "read_file", {"path": "a.txt"}) if entry == "send_tool_results" else None
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: reply_body(provider.api_protocol, call=call))
    provider.send_turn("first", [READ])
    before = copy.deepcopy(provider._messages)
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise api_transport.GenerationUnknownError("response lost")

    monkeypatch.setattr(api_transport, "generate", fail)
    with pytest.raises(api_transport.GenerationUnknownError):
        if entry == "send_tool_results":
            provider.send_tool_results([ProviderToolResult(call.id, "contents")], [READ])
        elif entry == "send_turn":
            provider.send_turn("second", [READ])
        else:
            provider.send("second")
    assert provider._messages == before
    assert len(calls) == 1


def test_plain_truncation_retains_input_without_committing_partial_answer(provider, monkeypatch):
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: reply_body(provider.api_protocol, limited=True))
    with pytest.raises(OutputLengthError):
        provider.send("do not accept a partial answer")
    assert provider._messages == [{"role": "user", "content": "do not accept a partial answer"}]


def test_native_exchange_updates_reasoning_for_the_latest_reply(provider, monkeypatch):
    replies = iter([reply_body(provider.api_protocol, reasoning="old reasoning"),
                    reply_body(provider.api_protocol, reasoning="new reasoning")])
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: next(replies))
    provider.send("first")
    turn = provider.send_turn("second", [READ])
    assert turn.reasoning == "new reasoning"
    assert provider.reasoning_for_reply("answer") == "new reasoning"


def test_unrequested_native_calls_are_not_silently_accepted_as_plain_text(provider, monkeypatch):
    call = ProviderToolCall(" exact id ", "read_file", {"path": "a.txt"})
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: reply_body(provider.api_protocol, call=call))
    with pytest.raises(RuntimeError, match="text-only"):
        provider.send("answer with text")
    assert provider._messages == [{"role": "user", "content": "answer with text"}]


def test_native_call_and_result_are_committed_once_with_exact_identity(provider, monkeypatch):
    call = ProviderToolCall(" exact id ", "read_file", {"path": "a.txt"})
    replies = iter([reply_body(provider.api_protocol, call=call), reply_body(provider.api_protocol)])
    sent = []

    def generate(endpoint, payload, *args, **kwargs):
        sent.append(copy.deepcopy(payload))
        return next(replies)

    monkeypatch.setattr(api_transport, "generate", generate)
    assert provider.send_turn("read", [READ]).tool_calls == (call,)
    assert provider.send_tool_results([ProviderToolResult(call.id, "contents")], [READ]).text == "answer"
    history = sent[1].get("messages", sent[1].get("input"))
    results = [item for item in history if item.get("role") == "tool" or item.get("type") == "function_call_output"]
    assert len(results) == 1
    assert results[0].get("tool_call_id", results[0].get("call_id")) == call.id
    assert len(sent) == 2


def test_cancelled_inflight_exchange_cannot_commit_and_other_send_is_rejected(provider, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    failures = []

    def generate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return reply_body(provider.api_protocol)

    def send():
        try:
            provider.send("first")
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(api_transport, "generate", generate)
    thread = threading.Thread(target=send)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="busy"):
            provider.send("second")
        provider.close()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], api_transport.GenerationUnknownError)
    assert not provider.has_transport_history


def test_shared_runtime_budget_does_not_query_local_capabilities(monkeypatch):
    monkeypatch.setattr("codey.providers.capabilities.capability_for", lambda _: pytest.fail("Local budget leaked"))
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", context_window_tokens=4096,
                           context_reserve_tokens=1024, context_keep_recent_tokens=2048)
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: reply_body(provider.api_protocol))
    assert provider.send("hello") == "answer"


@pytest.mark.parametrize("malformed_calls", [{}, "", 0, False])
def test_malformed_empty_chat_tool_calls_cannot_be_committed_as_plain_text(monkeypatch, malformed_calls):
    provider = ApiProvider("http://fixture.invalid/v1", "fixture")
    body = reply_body(provider.api_protocol)
    body["choices"][0]["message"]["tool_calls"] = malformed_calls
    monkeypatch.setattr(api_transport, "generate", lambda *a, **k: body)
    with pytest.raises(RuntimeError, match="malformed tool_calls"):
        provider.send("text only")
    assert provider._messages == [{"role": "user", "content": "text only"}]
