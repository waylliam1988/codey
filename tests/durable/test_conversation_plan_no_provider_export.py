from __future__ import annotations

from types import SimpleNamespace

from codey.agents.handoff import ConversationContext
from codey.durable import DurableSession, MemoryDurableStore
from codey.operations.conversation_plan import build_conversation_plan
from codey.providers.api_provider import ApiProvider


def test_fresh_recovery_never_reads_provider_work_state_export():
    durable = DurableSession(MemoryDurableStore())
    durable.create_conversation("chat")
    durable.append_message("chat", "assistant_message", {"text": "canonical durable answer"})
    provider = ApiProvider("http://localhost:9/v1", "fixture")
    called = False

    def forbidden_export():
        nonlocal called
        called = True
        raise AssertionError("provider export is not a recovery source")

    provider.export_work_state = forbidden_export
    state = SimpleNamespace(durable=durable, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="local", provider=provider,
        conversation=ConversationContext(), task_kind="chat", project=None, task="next", continue_task=False,
        trace=None, pure=True)
    assert not called
    assert plan.window_action is not None
    assert any(message.get("content") == "canonical durable answer"
               for message in plan.window_action.context.messages)
    provider.close()
    durable.close()
