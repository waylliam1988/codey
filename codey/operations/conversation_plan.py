from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_handoff,
    render_recovered_handoff,
)
from codey.task.kind import conversation_mode
from codey.runtime.prompt_envelope import record_provider_send_prompt


@dataclass(frozen=True)
class ConversationPlan:
    conversation: ConversationContext
    fresh_chat: bool
    handoff: str
    research_handoff: str
    prior_snapshot: ConversationSnapshot
    recovered_owner_prompt: str
    provider_session_changed: bool


def build_conversation_plan(
    *,
    state: Any,
    session_id: str,
    provider_id: str,
    provider: Any,
    conversation: ConversationContext,
    task_kind: str,
    project: str | None,
    task: str,
    continue_task: bool,
    trace: Any | None,
) -> ConversationPlan:
    mode = conversation_mode(task_kind, project)
    project_text = _project_text(project)
    provider_session_changed = state.provider_session_changed(
        provider_id,
        session_id,
    )
    can_summarize_current_chat = (
        conversation.initialized
        and provider_id == conversation.provider_id
        and not provider_session_changed
    )
    fresh_chat, handoff = conversation.plan_request(
        provider_id=provider_id,
        mode=mode,
        project=project_text,
        force_rollover=continue_task or provider_session_changed,
        next_prompt=task,
    )
    if fresh_chat and can_summarize_current_chat:

        def send_handoff_summary(summary_prompt: str) -> str:
            record_provider_send_prompt(
                trace,
                name="conversation_handoff_summary_prompt",
                text=summary_prompt,
                purpose="conversation handoff summary prompt sent to provider",
                source_ref="provider_send:conversation_handoff_summary",
                capability_id="conversation_handoff",
            )
            return provider.send(summary_prompt)

        handoff = conversation.prepare_model_handoff(send_handoff_summary)
    prior_snapshot = conversation.snapshot
    visible_excerpt = ""
    if fresh_chat or task_kind in {"research", "hybrid"}:
        try:
            visible_excerpt = state.visible_session_excerpt(
                session_id,
                current_request=task,
            )
        except Exception:
            visible_excerpt = ""
    research_handoff = ""
    if task_kind in {"research", "hybrid"}:
        if handoff or visible_excerpt:
            research_handoff = render_recovered_handoff(
                prior_snapshot,
                visible_excerpt,
            )
        elif prior_snapshot.to_payload():
            research_handoff = render_handoff(prior_snapshot)
    conversation.update_snapshot(replace(
        prior_snapshot,
        mode=mode,
        goal=prior_snapshot.goal or task,
        project=project_text,
        provider_id=prior_snapshot.provider_id,
        changed_files=prior_snapshot.changed_files,
        checks_passed=prior_snapshot.checks_passed,
        summary=prior_snapshot.summary,
        blocker=prior_snapshot.blocker,
        latest_user=task if mode == "chat" else "",
        latest_reply=prior_snapshot.latest_reply if mode == "chat" else "",
        conversation_summary=prior_snapshot.conversation_summary,
    ))
    recovered_owner_prompt = ""
    if fresh_chat:
        if handoff or visible_excerpt:
            handoff = render_recovered_handoff(
                prior_snapshot,
                visible_excerpt,
            )
        if visible_excerpt:
            recovered_owner_prompt = handoff
    return ConversationPlan(
        conversation=conversation,
        fresh_chat=fresh_chat,
        handoff=handoff,
        research_handoff=research_handoff,
        prior_snapshot=prior_snapshot,
        recovered_owner_prompt=recovered_owner_prompt,
        provider_session_changed=provider_session_changed,
    )


def _project_text(project: str | None) -> str:
    if not project:
        return ""
    from pathlib import Path

    return str(Path(project).expanduser().resolve())


__all__ = ["ConversationPlan", "build_conversation_plan"]
