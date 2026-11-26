"""A cold transport restores facts without claiming a provider binding change."""
from contextlib import closing
from dataclasses import replace

import pytest

from codey.agents.handoff import ConversationContext
from codey.app.context import AppContext
from codey.operations.conversation_plan import build_conversation_plan
from codey.operations.conversation_window import OpenWindow, WindowOpenReason
from codey.providers.api_provider import ApiProvider


def test_recreated_transport_forces_fact_handoff_without_sending_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    conversation = ConversationContext()
    conversation.begin_window("local", "chat")
    conversation.update_snapshot(replace(conversation.snapshot, goal="keep settled work", summary="probe.txt was already written"))
    provider = ApiProvider("http://localhost:9/v1", "fixture")

    def fail_send(*args: object, **kwargs: object) -> str:
        raise AssertionError("must not summarize absent history")

    monkeypatch.setattr(provider, "send", fail_send)
    with closing(AppContext(tmp_path_factory.mktemp("cold"))) as state, closing(provider):
        plan = build_conversation_plan(state=state, session_id="session", provider_id="local", provider=provider,
                                       conversation=conversation, task_kind="chat", project=None, task="continue",
                                       continue_task=False, trace=None)
        assert plan.fresh_chat
        assert not plan.provider_session_changed
        assert isinstance(plan.window_action, OpenWindow)
        assert plan.window_action.reason is WindowOpenReason.TRANSPORT_RECOVERY
        assert not plan.needs_model_summary
        assert "probe.txt was already written" in plan.handoff
        assert provider._messages == []
