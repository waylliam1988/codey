"""Selected API budgets, rather than website estimates, govern admission."""
from types import SimpleNamespace

import pytest

from codey.agents.handoff import ConversationContext
from codey.operations.conversation_plan import build_conversation_plan
from codey.providers.api_provider import ApiProvider


def test_selected_262k_window_is_not_replaced_by_static_32k_capability():
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_window_tokens=262144,
                           context_reserve_tokens=32768, context_keep_recent_tokens=32000)
    provider._messages = [{"role": "user", "content": "history"}]
    conversation = ConversationContext()
    conversation.begin_window("local", "chat")
    conversation.estimated_context_tokens = 25000
    provider.send = lambda *_: pytest.fail("API character estimates must not trigger a summary request")
    state = SimpleNamespace(provider_session_changed=lambda *_: False)
    plan = build_conversation_plan(state=state, session_id="s", provider_id="local", provider=provider,
                                   conversation=conversation, task_kind="chat", project=None,
                                   task="continue", continue_task=False, trace=None)
    assert conversation.hard_limit == 262144 - 32768
    assert not plan.fresh_chat


def test_api_static_capability_carries_no_model_capacity():
    from codey.providers.capabilities import capability_for

    assert capability_for("local").context_window_tokens == 0
    assert capability_for("zen").context_window_tokens == 0


def test_api_plan_without_a_resolved_budget_is_rejected():
    state = SimpleNamespace(provider_session_changed=lambda *_: False)
    with pytest.raises(ValueError, match="budget"):
        build_conversation_plan(state=state, session_id="s", provider_id="local", provider=object(),
                                conversation=ConversationContext(), task_kind="chat", project=None,
                                task="continue", continue_task=False, trace=None)


def test_output_limit_cannot_silently_expand_admitted_reservation():
    from codey.runtime.core.api_selection import ApiRunSelection

    with pytest.raises(ValueError):
        ApiRunSelection("local", "revision", "fixture", "openai-completions", True, output_tokens=9000)
    with pytest.raises(ValueError):
        ApiProvider("http://localhost:9/v1", "fixture", output_tokens=9000)
