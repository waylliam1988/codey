from types import SimpleNamespace

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.durable import DurableSession, MemoryDurableStore
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.conversation_window import OpenWindow, WindowOpenReason, activate_window
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding


def test_edit_activates_existing_window_boundary_with_only_retained_prefix():
    durable = DurableSession(MemoryDurableStore())
    for name in ("prefix", "removed"):
        durable.admit_input(conversation_id="chat", request_id=name, task_id=name, text=name, provider_snapshot={})
        durable.complete_task(name, f"answer:{name}", state={})
    source = durable.task("removed").state["input_entry_id"]
    durable.admit_input(conversation_id="chat", request_id="edit", task_id="edit", text="replacement", provider_snapshot={},
                        task_state={"input": {"kind": "edit_user_message", "entry_id": source,
                                              "text": "replacement", "expected_revision": durable.store.head("chat")[1]}})

    class Provider:
        context_budget = None
        has_transport_history = True
        def __init__(self):
            self.resets = 0
            self.messages = ["removed protocol history"]
            self._messages = [{"tool_calls": [{"id": "aborted"}]}]
            self._codec = SimpleNamespace(pending_calls=lambda _: ["aborted"])
        def load_durable_context(self, messages):
            self.messages = messages
        def reset_window(self):
            self.resets += 1
            self._messages = []
            return ProtocolWindowRef.new("chat", ProviderBinding("deepseek", "rev", "model", "native", "history"))

    provider = Provider()
    conversation = ConversationContext(provider_id="deepseek", mode="chat", initialized=True,
        handoff_summary="REMOVED SUMMARY", snapshot=ConversationSnapshot(mode="chat", goal="REMOVED GOAL",
                latest_reply="REMOVED REPLY", conversation_summary="REMOVED SUMMARY"))
    state = SimpleNamespace(durable=durable, provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="chat", provider_id="deepseek", provider=provider,
        conversation=conversation, task_kind="chat", project=None, task="replacement", continue_task=False,
        trace=None, pure=True, task_id="edit")
    assert plan.fresh_chat and isinstance(plan.window_action, OpenWindow)
    assert plan.window_action.reason is WindowOpenReason.HISTORY_EDIT
    assert not plan.needs_model_summary
    assert "REMOVED" not in plan.handoff
    assert conversation.snapshot.latest_reply == "REMOVED REPLY"  # Planning has no side effects.
    activate_window(provider, plan.window_action)
    plan.apply()
    assert provider.resets == 1
    assert "prefix" in str(provider.messages)
    assert "removed" not in str(provider.messages)
    assert "replacement" not in str(provider.messages)  # Sent exactly once by the existing task runner.
    assert conversation.snapshot.latest_reply == ""
    durable.close()
