from __future__ import annotations

import json
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from unittest import mock
from urllib.request import Request

import pytest

from codey.operations.kernel_events import _emit_turn_event
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, ProviderToolDefinition
from codey.runtime.observe.events import RunEvent, run_event_ui_payload
from codey.utils.object_fields import decoded_object, require_object_fields, require_object_rows


class _Response(BytesIO):
    headers: Mapping[str, str] = {"Content-Type": "application/json"}


def response(message: Mapping[str, object], finish: str = "stop") -> _Response:
    payload: dict[str, object] = {
        "choices": [{"finish_reason": finish, "message": message}],
    }
    return _Response(json.dumps(payload).encode())


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
    stored: object = provider._messages[-1]
    assert require_object_fields(stored)["reasoning_content"] == "Inspect first."


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


def test_text_json_tool_reply_with_reasoning_remains_executable_after_receipt_roundtrip() -> None:
    from codey.operations.kernel.kernel_protocol import normalize_turn
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.response_record import decode_reply, encode_reply

    provider = ApiProvider("http://localhost:9/v1", "test")
    text = '{"tool":"web_search","args":{"query":"official source"}}'
    with mock.patch("codey.providers.api_transport.open_request", return_value=response({
        "content": text, "reasoning_content": "Inspect official sources first.",
    })):
        reply = provider.send("research")
    normalized = provider.normalize_reply(reply)
    assert isinstance(normalized, AssistantTurn) and normalized.reasoning == "Inspect official sources first."
    restored = decode_reply(encode_reply(normalized))
    assert isinstance(restored, AssistantTurn)
    plan = normalize_turn(restored, policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    assert not plan.protocol_error and plan.control is not None and plan.control.kind == "continue"
    assert len(plan.calls) == 1 and plan.calls[0].name == "web_search"
    assert plan.calls[0].args == {"query": "official source"}
    denied = normalize_turn(restored, policy=TaskPolicy(grants=frozenset({"control"})))
    assert denied.protocol_error_kind == "disallowed_tool" and not denied.calls


def test_unmarked_native_prose_and_reasoning_do_not_become_text_tool_requests() -> None:
    from codey.operations.kernel.kernel_protocol import normalize_turn

    plan = normalize_turn(AssistantTurn(text='{"tool":"web_search","args":{"query":"example"}}',
                                      reasoning="An example only."))
    assert not plan.calls and plan.control is not None and plan.control.kind == "done"


def test_reasoning_reaches_existing_turn_event_without_changing_tool_text() -> None:
    seen: list[RunEvent] = []
    _emit_turn_event(seen.append, 2, AssistantTurn(text="answer", reasoning="Think safely."))
    payload = run_event_ui_payload("run1", "chat1", seen[0])
    assert payload is not None
    assert payload["reasoning"] == "Think safely."
    assert payload["turn"] == 2
    assert seen[0].reply == "answer"
    started = run_event_ui_payload("run1", "chat1", RunEvent.turn_started(3, "answer"))
    assert started is not None and "reasoning" not in started


def test_thinking_options_are_explicit_and_not_sent_to_every_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    ordinary = ApiProvider("http://localhost:9/v1", "test")
    thinking = ApiProvider("http://localhost:9/v1", "test", thinking_enabled=True)
    sent: list[dict[str, object]] = []

    def open_request(request: Request, timeout: float, **kwargs: object) -> _Response:
        assert isinstance(request.data, bytes)
        sent.append(decoded_object(request.data.decode("utf-8")))
        return response({"content": "hello"})

    monkeypatch.setattr("codey.providers.api_transport.open_request", open_request)
    assert ordinary.send("hello") == thinking.send("hello") == "hello"
    assert len(sent) == 2
    assert "chat_template_kwargs" not in sent[0]
    assert sent[1]["chat_template_kwargs"] == {"enable_thinking": True}


def test_effort_wire_selection_does_not_emit_a_second_thinking_control() -> None:
    from codey.providers import api_chat
    from codey.providers.api_codec import GenerationSettings

    payload = api_chat.build_payload([], None, GenerationSettings(
        "zen-fixture", False, 0.0, None, "high", "auto", None,
    ))
    assert payload["reasoning_effort"] == "high"
    assert "chat_template_kwargs" not in payload


def test_reasoning_counts_toward_the_next_request_context_budget() -> None:
    from codey.providers.api_metering import estimate_request

    ordinary = {"role": "assistant", "content": "Done"}
    reasoning = {**ordinary, "reasoning_content": "Inspect carefully. " * 1000}
    reasoning_payload: dict[str, object] = {"messages": [reasoning]}
    ordinary_payload: dict[str, object] = {"messages": [ordinary]}
    reasoning_tokens = estimate_request(reasoning_payload, deadline=0).value
    ordinary_tokens = estimate_request(ordinary_payload, deadline=0).value
    assert reasoning_tokens is not None and ordinary_tokens is not None
    assert reasoning_tokens > ordinary_tokens + 1000


def test_plain_reply_without_reasoning_cannot_reuse_previous_thoughts() -> None:
    provider = ApiProvider("http://localhost:9/v1", "test")
    responses: list[_Response] = [
        response({"content": "Same answer", "reasoning_content": "Previous thought"}),
        response({"content": "Same answer"}),
    ]
    with mock.patch("codey.providers.api_transport.open_request", side_effect=responses):
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


def test_process_state_and_reasoning_survive_ui_store_roundtrip(tmp_path: Path) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    reasoning = "Thought text. " * 150
    rows: list[dict[str, object]] = [{"type": "turn", "runId": "r", "n": 1, "reasoning": reasoning},
            {"type": "tool", "runId": "r", "toolName": "read_file", "turn": 1},
            {"type": "run_state", "runId": "r", "state": "done"}]
    # UI state is only session metadata; canonical messages belong to the
    # durable session store and must survive independently of that projection.
    store.save({"active_id": "s", "sessions": [{"id": "s", "title": "Chat", "provider": "local"}], "projects": []}, base_revision=0)
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        durable.create_conversation("s")
        for row in rows:
            durable.append_message("s", "display_message", row)
        restored = require_object_rows(durable.history_page("s")["items"])
        assert restored[0]["reasoning"] == reasoning
        assert restored[1]["toolName"] == "read_file" and restored[1]["turn"] == 1
        assert restored[2]["state"] == "done"
    finally:
        durable.close()
