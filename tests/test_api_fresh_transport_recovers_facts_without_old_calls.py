"""A new API transport recovers facts without summarizing an absent history."""
from dataclasses import replace
from types import SimpleNamespace

from codey.agents.handoff import ConversationContext
from codey.operations.conversation_plan import build_conversation_plan
from codey.providers.api_provider import ApiProvider


def test_recreated_transport_forces_fact_handoff_without_sending_summary():
    conversation = ConversationContext()
    conversation.begin_window("local", "chat")
    conversation.update_snapshot(replace(conversation.snapshot, goal="keep settled work", summary="probe.txt was already written"))
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    provider.send = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not summarize absent history"))
    state = SimpleNamespace(provider_session_changed=lambda *a: False, visible_session_excerpt=lambda *a, **k: "")
    plan = build_conversation_plan(state=state, session_id="session", provider_id="local", provider=provider,
                                   conversation=conversation, task_kind="chat", project=None, task="continue",
                                   continue_task=False, trace=None)
    assert plan.fresh_chat
    assert plan.provider_session_changed
    assert "probe.txt was already written" in plan.handoff
    assert provider._messages == []
