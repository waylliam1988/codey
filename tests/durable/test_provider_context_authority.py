import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.durable import DurableSession, MemoryDurableStore
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.conversation_window import activate_window
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolDefinition
from codey.providers.token_accounting import RequestContextCount


def test_repeated_prompt_text_does_not_erase_a_previous_user_input() -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    session.append_message("chat", kind="user_message", payload={"text": "again"})
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False,
                            visible_session_excerpt=Mock(return_value="forged UI answer"))
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="again", continue_task=False, trace=None)
    activate_window(provider, plan.window_action)
    assert any(message.get("content") == "again" for message in provider._messages)
    state.visible_session_excerpt.assert_not_called()


def test_provider_context_stops_at_this_submission_before_later_queued_inputs() -> None:
    session = DurableSession(MemoryDurableStore())
    session.admit_input(conversation_id="chat", request_id="first", task_id="first", text="prior",
                        provider_snapshot={"provider_id": "local"})
    session.complete_task("first", "prior answer", state={})
    session.admit_input(conversation_id="chat", request_id="current", task_id="current", text="again",
                        provider_snapshot={"provider_id": "local"})
    session.admit_input(conversation_id="chat", request_id="later", task_id="later", text="not yet allowed",
                        provider_snapshot={"provider_id": "local"})
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False,
                            visible_session_excerpt=Mock(return_value="forged UI answer"))
    plan = build_conversation_plan(state=state, session_id="chat", task_id="current", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="research", project=None, task="again", continue_task=False, trace=None)
    activate_window(provider, plan.window_action)
    contents = [message.get("content") for message in provider._messages]
    assert "prior" in contents and "prior answer" in contents
    assert "again" not in contents and "not yet allowed" not in contents
    state.visible_session_excerpt.assert_not_called()


def test_pure_planning_defers_context_loading_until_window_activation() -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    session.append_message("chat", "assistant_message", {"text": "durable previous answer"})
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="next", continue_task=False, trace=None,
        pure=True)
    assert provider._messages == []
    activate_window(provider, plan.window_action)
    assert any(message.get("content") == "durable previous answer" for message in provider._messages)


def test_browser_handoff_uses_durable_history_without_an_explicit_replay_loader() -> None:
    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    session.append_message("chat", "assistant_message", {"text": "canonical answer"})
    class BrowserHandoffProvider:
        """Concrete browser lifecycle surface used by the activation boundary."""

        def __init__(self) -> None:
            self.opened = False

        def new_chat(self) -> None:
            self.opened = True

    provider = BrowserHandoffProvider()
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="deepseek", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="next", continue_task=False, trace=None,
        pure=True)
    assert "canonical answer" in plan.handoff
    activate_window(provider, plan.window_action)
    assert provider.opened


def test_cold_window_preserves_durable_handoff_facts_without_visible_messages() -> None:
    conversation = ConversationContext()
    conversation.snapshot = ConversationSnapshot(mode="chat", goal="preserved project goal", latest_reply="SQLite chosen")
    state = SimpleNamespace(provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="deepseek", provider=Mock(),
        conversation=conversation, task_kind="chat", project=None, task="next", continue_task=False, trace=None,
        pure=True)
    assert "preserved project goal" in plan.handoff
    assert "SQLite chosen" in plan.handoff


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_first_actual_request_replays_paired_tools_in_the_selected_protocol(monkeypatch, protocol) -> None:
    from codey.providers import api_transport

    session = DurableSession(MemoryDurableStore())
    session.create_conversation("chat")
    session.append_message("chat", "user_message", {"text": "previous input"})
    session.append_message("chat", "tool_call", {"tool_calls": [
        {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"path":"x.py"}'}}
    ]})
    session.append_message("chat", "tool_result", {"call_id": "call-1", "content": "old result", "is_error": True})
    provider = ApiProvider("http://localhost:9/v1", "fixture", api_protocol=protocol, system_prompt="trusted system")
    counted, sent = [], []

    def count(payload, *, deadline):
        counted.append(json.loads(json.dumps(payload)))
        return RequestContextCount(100, "tokenizer")

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request, timeout):
        sent.append(json.loads(request.data))
        body = ({"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]}
                if protocol == "openai-completions" else {"status": "completed", "output": [
                    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}
                ]})
        return Response(json.dumps(body).encode())

    provider.request_counter = count
    monkeypatch.setattr(api_transport, "open_request", send)
    state = SimpleNamespace(durable=session, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="next", continue_task=False, trace=None,
        pure=True)
    activate_window(provider, plan.window_action)
    assert provider.send_turn("next", [ProviderToolDefinition("read", "read file", {})]).text == "answer"
    assert counted[-1] == sent[0]
    items = sent[0]["messages" if protocol == "openai-completions" else "input"]
    assert items[0] == {"role": "system", "content": "trusted system"}
    assert items[1] == {"role": "user", "content": "previous input"}
    assert "tools" in sent[0] and sent[0].get("max_tokens", sent[0].get("max_output_tokens")) == 8192
    if protocol == "openai-responses":
        assert items[2]["type"] == "function_call" and items[2]["call_id"] == "call-1"
        assert items[3] == {"type": "function_call_output", "call_id": "call-1", "output": "old result"}
    else:
        assert items[2]["tool_calls"][0]["id"] == "call-1"
        assert items[3] == {"role": "tool", "tool_call_id": "call-1", "content": "old result"}


def test_conflicting_context_activation_is_rejected_instead_of_silently_ignored() -> None:
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.load_durable_context([{"role": "user", "content": "original"}])
    with pytest.raises(ValueError, match="context"):
        provider.load_durable_context([{"role": "user", "content": "different branch"}])
    assert provider._messages == [{"role": "user", "content": "original"}]
