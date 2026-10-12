"""Regression tests for shell approval continuation and protocol window reuse."""

from types import SimpleNamespace

from codey.agents.handoff import ConversationContext
from codey.operations.conversation_plan import build_conversation_plan
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolResult


def test_planned_resume_keeps_pending_call_history() -> None:
    """A continuation run must not turn into an implicit fresh protocol window."""
    provider = ApiProvider("http://127.0.0.1:9/v1", "fixture")
    provider._messages = [
        {"type": "function_call", "call_id": "call-shell-1", "name": "shell", "arguments": "{}"},
    ]
    conversation = ConversationContext()
    conversation.begin_window("local", "chat")
    state = SimpleNamespace(provider_session_changed=lambda *_: False)

    plan = build_conversation_plan(
        state=state,
        session_id="session-1",
        provider_id="local",
        provider=provider,
        conversation=conversation,
        task_kind="chat",
        project=None,
        task="continue after approval",
        continue_task=True,
        trace=None,
    )

    assert plan.fresh_chat is False
    assert provider._messages[0]["call_id"] == "call-shell-1"


def test_responses_result_is_sent_in_original_window(monkeypatch, tmp_path) -> None:
    provider = ApiProvider("http://fixture/v1", "fixture", api_protocol="openai-responses")
    provider.bind_context("session-1", tmp_path)
    provider._messages = [{
        "type": "function_call", "call_id": "call-shell-1", "name": "shell", "arguments": "{}",
    }]
    window = provider.window_ref
    assert window is not None
    seen = []

    def fake_generate(_endpoint, payload, _headers, **_kwargs):
        seen.append(payload)
        return {"status": "completed", "output": [{
            "type": "message", "status": "completed", "content": [{"type": "output_text", "text": "done"}],
        }]}

    monkeypatch.setattr("codey.providers.api_transport.generate", fake_generate)
    turn = provider.send_tool_results_bound(
        [ProviderToolResult("call-shell-1", "exit 0")], window,
    )
    assert turn.text == "done"
    assert seen[0]["input"][-1]["call_id"] == "call-shell-1"

