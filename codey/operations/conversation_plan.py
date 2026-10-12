from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, cast

from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_handoff,
    render_recovered_handoff,
)
from codey.operations.conversation_window import OpenWindow, ReuseWindow, WindowAction, WindowOpenReason
from codey.operations.task_state import TaskState
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.protocol_window import ProtocolWindowRef
from codey.runtime.observe.prompt_envelope import record_provider_send_prompt
from codey.task.kind import conversation_mode


@dataclass(frozen=True)
class ConversationPlan:
    conversation: ConversationContext
    fresh_chat: bool
    handoff: str
    research_handoff: str
    prior_snapshot: ConversationSnapshot
    recovered_owner_prompt: str
    provider_session_changed: bool
    window_action: WindowAction | None = None


def build_conversation_plan(  # noqa: C901, PLR0912
    *,
    state: TaskState,
    session_id: str,
    provider_id: str,
    provider: Any,
    conversation: ConversationContext,
    task_kind: str,
    project: str | None,
    task: str,
    continue_task: bool,
    trace: Any | None,
) -> ConversationPlan:  # noqa: C901, PLR0912
    mode = conversation_mode(task_kind, project)
    project_text = _project_text(project)
    budget = getattr(provider, "context_budget", None)
    if isinstance(budget, ContextBudget):
        conversation.hard_limit = budget.input_limit
        conversation.reserve_tokens = budget.output_tokens + budget.safety_tokens
        conversation.keep_recent_tokens = budget.keep_recent_tokens
        conversation.estimated_context_tokens = None
    else:
        from codey.providers.capabilities import capability_for
        from codey.providers.catalog import API_CONNECTIONS

        if provider_id in API_CONNECTIONS:
            raise ValueError("API connection requires a resolved context budget")

        capability = capability_for(provider_id)
        window = capability.context_window_tokens
        reserve = capability.context_reserve_tokens
        keep_recent = capability.context_keep_recent_tokens
        if window > reserve > 0:
            conversation.hard_limit = window - reserve
            conversation.reserve_tokens = reserve
        if keep_recent > 0:
            conversation.keep_recent_tokens = keep_recent
        if conversation.estimated_context_tokens is None:
            conversation.estimated_context_tokens = 0
    provider_session_changed = state.provider_session_changed(
        provider_id,
        session_id,
    ) or getattr(provider, "has_transport_history", None) is False
    can_summarize_current_chat = (
        conversation.initialized
        and provider_id == conversation.provider_id
        and not provider_session_changed
    )
    fresh_chat, handoff = conversation.plan_request(
        provider_id=provider_id,
        mode=mode,
        project=project_text,
        # A resumed run reuses the protocol window.  Opening a new window is a
        # separate explicit action and must never be inferred from continue_task.
        # A ResumeRun is already bound to the source window.  A fresh provider
        # wrapper for the same connection must not turn that explicit resume
        # into an implicit new-chat rollover.
        force_rollover=provider_session_changed and not continue_task,
        next_prompt=task,
    )
    if fresh_chat:
        pending_calls = getattr(getattr(provider, "_codec", None), "pending_calls", None)
        history = getattr(provider, "_messages", [])
        pending = pending_calls(history) if callable(pending_calls) else ()
        if isinstance(pending, (tuple, list, set, frozenset)) and pending:
            raise ValueError("cannot open a new protocol window while calls are pending")
    if fresh_chat and isinstance(budget, ContextBudget):
        export = getattr(provider, "export_work_state", None)
        work_state = export() if callable(export) else ""
        if work_state:
            handoff = "\n\n".join(part for part in (handoff, work_state) if part)
    if fresh_chat and can_summarize_current_chat and not isinstance(budget, ContextBudget):

        def send_handoff_summary(summary_prompt: str) -> str:
            record_provider_send_prompt(
                trace,
                name="conversation_handoff_summary_prompt",
                text=summary_prompt,
                purpose="conversation handoff summary prompt sent to provider",
                source_ref="provider_send:conversation_handoff_summary",
                capability_id="conversation_handoff",
            )
            return cast(str, provider.send(summary_prompt))

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
            preserved_work_state = work_state if isinstance(budget, ContextBudget) else ""
            handoff = render_recovered_handoff(
                prior_snapshot,
                visible_excerpt,
            )
            if preserved_work_state:
                handoff += "\n\n" + preserved_work_state
        if visible_excerpt:
            recovered_owner_prompt = handoff
    raw_window_ref = getattr(provider, "window_ref", None)
    window_ref = raw_window_ref if isinstance(raw_window_ref, ProtocolWindowRef) else None
    if not fresh_chat and window_ref is not None:
        window_action: WindowAction | None = ReuseWindow(window_ref)
    elif fresh_chat:
        window_action = OpenWindow(
            WindowOpenReason.MODEL_CHANGE if provider_session_changed else WindowOpenReason.CONTEXT_ROLLOVER,
            seed=handoff,
        )
    else:
        window_action = None
    return ConversationPlan(
        conversation=conversation,
        fresh_chat=fresh_chat,
        handoff=handoff,
        research_handoff=research_handoff,
        prior_snapshot=prior_snapshot,
        recovered_owner_prompt=recovered_owner_prompt,
        provider_session_changed=provider_session_changed,
        window_action=window_action,
    )


def _project_text(project: str | None) -> str:
    if not project:
        return ""
    from pathlib import Path

    return str(Path(project).expanduser().resolve())


__all__ = ["ConversationPlan", "build_conversation_plan"]
