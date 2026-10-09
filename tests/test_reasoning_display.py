from __future__ import annotations

import json
from unittest import mock

import pytest

from codey.operations.kernel_events import _emit_turn_event
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolDefinition
from codey.runtime.observe.events import RunEvent, run_event_ui_payload


def response(message: dict, finish: str = "stop") -> mock.MagicMock:
    result = mock.MagicMock()
    result.__enter__.return_value.read.return_value = json.dumps({
        "choices": [{"finish_reason": finish, "message": message}],
    }).encode()
    return result


def test_reasoning_is_separate_from_answer_and_preserved_for_tool_history() -> None:
    provider = ApiProvider("http://localhost:9/v1", "test")
    message = {"content": "", "reasoning_content": "Inspect first.", "tool_calls": [{
        "id": "c1", "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"app.py"}'},
    }]}
    with mock.patch("codey.providers.api_transport.open_request", return_value=response(message, "tool_calls")):
        turn = provider.send_turn("fix", [ProviderToolDefinition('done', '', {})])
    assert turn.reasoning == "Inspect first."
    assert turn.text == ""
    assert turn.tool_calls[0].name == "read_file"
    assert provider._messages[-1]["reasoning_content"] == "Inspect first."


@pytest.mark.parametrize("value", [None, "", " \n\t", {"text": "not a string"}])
def test_empty_or_malformed_reasoning_does_not_create_content(value: object) -> None:
    provider = ApiProvider("http://localhost:9/v1", "test")
    with mock.patch("codey.providers.api_transport.open_request", return_value=response({
        "content": "Answer", "reasoning_content": value,
    })):
        turn = provider.send_turn("question")
    assert turn.text == "Answer"
    assert turn.reasoning == ""


def test_plain_send_keeps_string_contract_and_exposes_only_matching_reasoning() -> None:
    provider = ApiProvider("http://localhost:9/v1", "test")
    with mock.patch("codey.providers.api_transport.open_request", return_value=response({
        "content": "391", "reasoning_content": "Calculate 17 times 23.",
    })):
        reply = provider.send("multiply")
    assert reply == "391"
    assert provider.reasoning_for_reply(reply) == "Calculate 17 times 23."
    assert provider.reasoning_for_reply("unrelated") == ""
    provider.new_chat()
    assert provider.reasoning_for_reply(reply) == ""


def test_reasoning_reaches_existing_turn_event_without_changing_tool_text() -> None:
    seen = []
    _emit_turn_event(seen.append, 2, AssistantTurn(text="answer", reasoning="Think safely."))
    payload = run_event_ui_payload("run1", "chat1", seen[0])
    assert payload["reasoning"] == "Think safely."
    assert payload["turn"] == 2
    assert seen[0].reply == "answer"
    assert "reasoning" not in run_event_ui_payload("run1", "chat1", RunEvent.turn_started(3, "answer"))


def test_thinking_options_are_explicit_and_not_sent_to_every_endpoint(monkeypatch) -> None:
    from tests.test_api_chat_native_turns_and_history import _install_fake

    ordinary = ApiProvider("http://localhost:9/v1", "test")
    thinking = ApiProvider("http://localhost:9/v1", "test", thinking_enabled=True)
    sent = _install_fake(monkeypatch, {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]})
    assert ordinary.send("hello") == thinking.send("hello") == "hello"
    assert len(sent) == 2
    assert "chat_template_kwargs" not in sent[0]
    assert sent[1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_reasoning_counts_toward_the_next_request_context_budget() -> None:
    from codey.providers.api_metering import estimate_request

    ordinary = {"role": "assistant", "content": "Done"}
    reasoning = {**ordinary, "reasoning_content": "Inspect carefully. " * 1000}
    assert estimate_request({"messages": [reasoning]}, deadline=0).value > estimate_request({"messages": [ordinary]}, deadline=0).value + 1000


def test_plain_reply_without_reasoning_cannot_reuse_previous_thoughts() -> None:
    provider = ApiProvider("http://localhost:9/v1", "test")
    with mock.patch("codey.providers.api_transport.open_request", side_effect=[
        response({"content": "Same answer", "reasoning_content": "Previous thought"}),
        response({"content": "Same answer"}),
    ]):
        provider.send("first")
        reply = provider.send("second")
    assert provider.reasoning_for_reply(reply) == ""


def test_thinking_configuration_roundtrip_and_target_isolation() -> None:
    from codey.providers.local_config import config_from_dict, config_to_payload, parse_local_config_update

    previous = config_from_dict({"schema_version": 1, "base_url": "http://localhost:5001/v1",
                                 "model": "m", "thinking_enabled": True})
    assert config_from_dict(config_to_payload(previous)).thinking_enabled is True
    same, error = parse_local_config_update({"base_url": previous.base_url, "model": "m"}, previous)
    assert not error and same is not None and same.thinking_enabled is True
    other, error = parse_local_config_update({"base_url": "http://localhost:9999/v1", "model": "m"}, previous)
    assert not error and other is not None and other.thinking_enabled is None
    invalid, error = parse_local_config_update({"base_url": previous.base_url, "model": "m",
                                               "thinking_enabled": "true"}, previous)
    assert invalid is None and error


def test_process_state_and_reasoning_survive_ui_store_roundtrip(tmp_path) -> None:
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    reasoning = "Thought text. " * 150
    rows = [{"type": "turn", "runId": "r", "n": 1, "reasoning": reasoning},
            {"type": "tool", "runId": "r", "toolName": "read_file", "turn": 1},
            {"type": "run_state", "runId": "r", "state": "done"}]
    store.save({"active_id": "s", "sessions": [{"id": "s", "title": "Chat", "provider": "local",
                 "messages": rows}], "projects": []}, base_revision=0)
    restored = store.load()["sessions"][0]["messages"]
    assert restored[0]["reasoning"] == reasoning
    assert restored[1]["toolName"] == "read_file" and restored[1]["turn"] == 1
    assert restored[2]["state"] == "done"
