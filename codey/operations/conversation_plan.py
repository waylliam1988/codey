from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any, cast

from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    _normalize_model_summary,
    render_handoff,
    render_recovered_handoff,
    render_summary_prompt,
)
from codey.operations.conversation_window import OpenWindow, ReuseWindow, WindowAction, WindowOpenReason
from codey.operations.task_state import TaskState
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.cancellation import TaskCancelled
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
    planned_conversation: ConversationContext | None = None
    needs_model_summary: bool = False

    def apply(self) -> None:
        """Commit a pure planning snapshot at the activation boundary."""
        planned = self.planned_conversation
        if planned is None or planned is self.conversation:
            return
        target = self.conversation
        for name in (
            "hard_limit", "reserve_tokens", "keep_recent_tokens", "estimated_context_tokens",
            "provider_id", "mode", "project", "initialized", "handoff_summary", "snapshot",
        ):
            setattr(target, name, copy.deepcopy(getattr(planned, name)))
        target._changed()

    def activate_model_summary(self, provider: Any, trace: Any | None) -> ConversationPlan:
        """Perform the provider side effect at the activation boundary."""
        if not self.needs_model_summary:
            return self
        conversation = self.conversation
        prompt = render_summary_prompt(self.prior_snapshot)
        record_provider_send_prompt(
            trace,
            name="conversation_handoff_summary_prompt",
            text=prompt,
            purpose="conversation handoff summary prompt sent to provider",
            source_ref="provider_send:conversation_handoff_summary",
            capability_id="conversation_handoff",
        )
        try:
            reply = provider.send(prompt)
        except TaskCancelled:
            raise
        except Exception:
            return self
        snapshot = replace(
            conversation.snapshot,
            conversation_summary=_normalize_model_summary(str(reply)),
        )
        conversation.snapshot = snapshot
        conversation._changed()
        handoff = render_handoff(snapshot)
        context = self.window_action.context if isinstance(self.window_action, OpenWindow) else None
        return replace(self, handoff=handoff,
                       window_action=OpenWindow(WindowOpenReason.CONTEXT_ROLLOVER, seed=handoff, context=context))


def build_conversation_plan(  # noqa: C901, PLR0912, PLR0915
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
    pure: bool = False,
    task_id: str | None = None,
) -> ConversationPlan:  # noqa: C901, PLR0912
    source_conversation = conversation
    if pure:
        conversation = replace(conversation, on_change=None)
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
    cold_transport = (callable(getattr(type(provider), "load_durable_context", None))
                      and getattr(provider, "has_transport_history", None) is False)
    provider_session_changed = state.provider_session_changed(
        provider_id,
        session_id,
    ) or getattr(provider, "has_transport_history", None) is False
    durable_handoff = ""
    durable_excerpt = ""
    replay_context = None
    durable = getattr(state, "durable", None)
    if durable is not None and durable.has_conversation(session_id):
        context_budget = max(1, int(getattr(conversation, "hard_limit", 32_768)))
        # Token counting is a provider capability, declared on its concrete
        # type. Dynamic attributes are deliberately ignored at this boundary.
        declared_counter = getattr(type(provider), "count_context_messages", None)
        token_counter = provider.count_context_messages if callable(declared_counter) else None
        input_entry_id = None
        if task_id is not None:
            checkpoint = durable.task(task_id)
            if checkpoint.conversation_id != session_id:
                raise ValueError("task belongs to another conversation")
            input_entry_id = checkpoint.state.get("resume_entry_id", checkpoint.state["input_entry_id"])
            if cold_transport and durable.effects(task_id):
                # A restarted protocol window replays committed exchanges,
                # including this task's tool calls and settled outcomes.
                input_entry_id = None
        durable_snapshot = durable.context_snapshot(
            session_id,
            token_budget=context_budget,
            before_entry_id=input_entry_id,
            token_counter=token_counter if callable(token_counter) else None,
        )
        durable_messages = list(durable_snapshot.messages)
        if durable_messages:
            durable_excerpt = _render_durable_handoff(durable_messages, durable_snapshot.digest)
        loader = getattr(type(provider), "load_durable_context", None)
        if callable(loader):
            replay_context = durable_snapshot
        if durable_messages and not callable(loader):
            durable_handoff = _render_durable_handoff(durable_messages, durable_snapshot.digest)
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
        force_rollover=cold_transport or (provider_session_changed and not continue_task),
        resume=continue_task and not cold_transport,
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
    if fresh_chat and durable_handoff:
        handoff = "\n\n".join(part for part in (handoff, durable_handoff) if part)
    if fresh_chat and can_summarize_current_chat and not isinstance(budget, ContextBudget) and not pure:

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
    visible_excerpt = durable_excerpt if replay_context is None else ""
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
    if fresh_chat and pure:
        handoff = render_recovered_handoff(prior_snapshot, visible_excerpt) if (handoff or visible_excerpt or prior_snapshot.to_payload()) else ""
        if isinstance(budget, ContextBudget) and work_state:
            handoff = "\n\n".join(part for part in (handoff, work_state) if part)
    elif fresh_chat:
        if handoff or visible_excerpt or prior_snapshot.to_payload():
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
            context=replay_context,
        )
    else:
        window_action = None
    return ConversationPlan(
        conversation=source_conversation if pure else conversation,
        fresh_chat=fresh_chat,
        handoff=handoff,
        research_handoff=research_handoff,
        prior_snapshot=prior_snapshot,
        recovered_owner_prompt=recovered_owner_prompt,
        provider_session_changed=provider_session_changed,
        window_action=window_action,
        needs_model_summary=(pure and fresh_chat and can_summarize_current_chat
                             and not isinstance(budget, ContextBudget)),
        planned_conversation=conversation if pure else None,
    )


def _project_text(project: str | None) -> str:
    if not project:
        return ""
    from pathlib import Path

    return str(Path(project).expanduser().resolve())


def _render_durable_handoff(messages: list[dict[str, object]], digest: str) -> str:
    lines = [f"Canonical conversation context (digest {digest[:16]}):"]
    for message in messages:
        role = str(message.get("role") or "system")
        content = str(message.get("content") or "").strip()
        if content:
            lines.append(f"{role}: {content[:4000]}")
    return "\n".join(lines)


def plan_conversation(**kwargs: Any) -> ConversationPlan:
    """Pure planning entry used by production dispatch.

    It computes the action and seed without provider I/O or mutation of the
    live conversation; :meth:`ConversationPlan.apply` is the activation step.
    """
    kwargs["pure"] = True
    return build_conversation_plan(**kwargs)


__all__ = ["ConversationPlan", "build_conversation_plan", "plan_conversation"]
