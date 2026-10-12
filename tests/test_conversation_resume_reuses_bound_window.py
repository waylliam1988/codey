"""A ResumeRun must not implicitly call a provider's new_chat hook."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from codey.agents.handoff import ConversationContext
from codey.operations.conversation_plan import build_conversation_plan


def test_resume_with_recreated_provider_keeps_existing_window() -> None:
    project = str(Path("/tmp/p").expanduser().resolve())
    conversation = ConversationContext(provider_id="deepseek", mode="project", project=project, initialized=True)
    provider = Mock()
    provider.context_budget = None
    provider.has_transport_history = True
    state = SimpleNamespace(provider_session_changed=lambda *_: True, visible_session_excerpt=lambda **_: "")

    plan = build_conversation_plan(
        state=state,
        session_id="s1",
        provider_id="deepseek",
        provider=provider,
        conversation=conversation,
        task_kind="project",
        project=project,
        task="Continue the unfinished task.",
        continue_task=True,
        trace=None,
    )

    assert plan.fresh_chat is False
    provider.new_chat.assert_not_called()
