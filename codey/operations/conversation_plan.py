from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import NotRequired, TypedDict, Unpack

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
from codey.providers.compat_transport import declared_callable, optional_callable
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.cancellation import TaskCancelled
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding
from codey.runtime.observe.prompt_envelope import record_provider_send_prompt
from codey.storage.managed_outputs import ManagedOutputStore
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

    def activate_model_summary(self, provider: object, trace: object) -> ConversationPlan:
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
            reply = _send_summary(provider, prompt)
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


def _send_summary(provider: object, prompt: str) -> str:
    send = optional_callable(provider, "send")
    if send is None:
        raise TypeError("provider cannot produce a conversation summary")
    return str(send(prompt))


def _previous_binding(state: TaskState, outputs: ManagedOutputStore | None, session_id: str) -> ProviderBinding | None:
    if outputs is None:
        return None
    from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact

    tasks = (task for task in reversed(state.durable.tasks()) if task.conversation_id == session_id and task.execution_kind == "model")
    for task in tasks:
        for effect in reversed(state.durable.settled_effects(task.task_id)):
            ref = effect.intent.get("request_ref")
            if effect.category != "provider_send" or effect.status != "ok" or not isinstance(ref, str) or not ref:
                continue
            request = read_provider_artifact(outputs, state.durable.artifact(ref), kind=REQUEST_KIND,
                session_id=session_id, run_id=task.task_id, effect_id=effect.effect_id)
            if request.get("window") is not None:
                return ProtocolWindowRef.from_payload(request["window"]).binding
    return None


def _window_reason(*, history_edit: bool, binding_changed: bool, cold: bool, initialized: bool,
                   has_durable_history: bool, budget_omissions: bool) -> WindowOpenReason:
    if history_edit:
        return WindowOpenReason.HISTORY_EDIT
    if binding_changed:
        return WindowOpenReason.MODEL_CHANGE
    if budget_omissions:
        return WindowOpenReason.CONTEXT_ROLLOVER
    if cold and (initialized or has_durable_history):
        return WindowOpenReason.TRANSPORT_RECOVERY
    return WindowOpenReason.CONTEXT_ROLLOVER if initialized else WindowOpenReason.FIRST_OPEN


def _model_or_protocol_changed(previous: ProviderBinding, current: ProviderBinding) -> bool:
    return ((previous.connection_id, previous.connection_revision, previous.model_id, previous.protocol)
            != (current.connection_id, current.connection_revision, current.model_id, current.protocol))


def build_conversation_plan(  # noqa: C901, PLR0912, PLR0915
    *,
    state: TaskState,
    session_id: str,
    provider_id: str,
    provider: object,
    conversation: ConversationContext,
    task_kind: str,
    project: str | None,
    task: str,
    continue_task: bool,
    trace: object,
    pure: bool = False,
    task_id: str | None = None,
    managed_outputs: ManagedOutputStore | None = None,
) -> ConversationPlan:  # noqa: C901, PLR0912
    source_conversation = conversation
    initialized = conversation.initialized
    if pure:
        conversation = replace(conversation, on_change=None)
    from codey.durable.session import DurableSession

    raw_durable: object = getattr(state, "durable", None)
    durable = raw_durable if isinstance(raw_durable, DurableSession) else None
    checkpoint = durable.task(task_id) if durable is not None and task_id is not None else None
    input_value = checkpoint.state.get("input") if checkpoint is not None else None
    history_edit = isinstance(input_value, Mapping) and input_value.get("kind") == "edit_user_message"
    if history_edit:
        conversation.initialized = False
        conversation.handoff_summary = ""
        conversation.snapshot = ConversationSnapshot(mode=conversation_mode(task_kind, project))
        conversation.estimated_context_tokens = 0
    mode = conversation_mode(task_kind, project)
    project_text = _project_text(project)
    budget: object = getattr(provider, "context_budget", None)
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
    raw_window_ref: object = getattr(provider, "window_ref", None)
    window_ref = raw_window_ref if isinstance(raw_window_ref, ProtocolWindowRef) else None
    loader = declared_callable(provider, "load_durable_context")
    cold_transport = loader is not None and getattr(provider, "has_transport_history", None) is False
    previous = _previous_binding(state, managed_outputs, session_id) if window_ref is not None and durable is not None else None
    binding_changed = (previous != window_ref.binding if previous is not None and window_ref is not None else
                       initialized and provider_id != conversation.provider_id)
    model_changed = (_model_or_protocol_changed(previous, window_ref.binding)
                     if previous is not None and window_ref is not None else binding_changed)
    provider_session_changed = binding_changed or (loader is None and state.provider_session_changed(provider_id, session_id))
    durable_handoff = ""
    durable_excerpt = ""
    replay_context = None
    if durable is not None and durable.has_conversation(session_id):
        context_budget = max(1, int(getattr(conversation, "hard_limit", 32_768)))
        # Token counting is a provider capability, declared on its concrete
        # type. Dynamic attributes are deliberately ignored at this boundary.
        declared_counter = declared_callable(provider, "count_context_messages")
        token_counter: Callable[[tuple[Mapping[str, object], ...]], int] | None = None
        if declared_counter is not None:
            def count(messages: tuple[Mapping[str, object], ...]) -> int:
                value = declared_counter(messages)
                if not isinstance(value, int):
                    raise ValueError("provider context counter returned an invalid count")
                return value
            token_counter = count
        input_entry_id: str | None = None
        if task_id is not None:
            checkpoint = durable.task(task_id)
            if checkpoint.conversation_id != session_id:
                raise ValueError("task belongs to another conversation")
            raw_entry = checkpoint.state.get("resume_entry_id", checkpoint.state["input_entry_id"])
            if not isinstance(raw_entry, str):
                raise ValueError("durable task has no input entry identity")
            input_entry_id = raw_entry
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
        if loader is not None:
            replay_context = durable_snapshot
        if durable_messages and loader is None:
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
        force_rollover=history_edit or cold_transport or (provider_session_changed and not continue_task),
        resume=continue_task and not cold_transport,
        next_prompt=task,
    )
    if fresh_chat:
        codec: object = getattr(provider, "_codec", None)
        pending_calls = optional_callable(codec, "pending_calls")
        history: object = getattr(provider, "_messages", [])
        pending = pending_calls(history) if pending_calls is not None else ()
        if not history_edit and isinstance(pending, (tuple, list, set, frozenset)) and pending:
            raise ValueError("cannot open a new protocol window while calls are pending")
    # Durable context is the sole recovery source. Provider protocol state is
    # reconstructed at activation and never serialized into handoff text.
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
            return _send_summary(provider, summary_prompt)

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
    elif fresh_chat:
        if handoff or visible_excerpt or prior_snapshot.to_payload():
            handoff = render_recovered_handoff(
                prior_snapshot,
                visible_excerpt,
            )
        if visible_excerpt:
            recovered_owner_prompt = handoff
    if fresh_chat and replay_context is not None and replay_context.omitted_handoff:
        handoff = "\n\n".join(part for part in (handoff, replay_context.omitted_handoff) if part)
        if task_kind in {"research", "hybrid"}:
            research_handoff = "\n\n".join(part for part in (research_handoff, replay_context.omitted_handoff) if part)
    if not fresh_chat and window_ref is not None:
        window_action: WindowAction | None = ReuseWindow(window_ref)
    elif fresh_chat:
        window_action = OpenWindow(
            _window_reason(history_edit=history_edit, binding_changed=model_changed, cold=cold_transport,
                initialized=initialized, has_durable_history=bool(replay_context and replay_context.messages),
                budget_omissions=bool(replay_context and replay_context.omitted_handoff)),
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


def _render_durable_handoff(messages: list[Mapping[str, object]], digest: str) -> str:
    lines = [f"Canonical conversation context (digest {digest[:16]}):"]
    for message in messages:
        role = str(message.get("role") or "system")
        content = str(message.get("content") or "").strip()
        if message.get("attachments"):
            from codey.task.attachments import attachment_message, validate_attachments

            content = str(attachment_message(content, validate_attachments(message["attachments"]), protocol="openai-completions", images=False))
        if content:
            lines.append(f"{role}: {content[:4000]}")
    return "\n".join(lines)


class _PlanArguments(TypedDict):
    state: TaskState
    session_id: str
    provider_id: str
    provider: object
    conversation: ConversationContext
    task_kind: str
    project: str | None
    task: str
    continue_task: bool
    trace: object
    task_id: NotRequired[str | None]
    managed_outputs: NotRequired[ManagedOutputStore | None]


def plan_conversation(**kwargs: Unpack[_PlanArguments]) -> ConversationPlan:
    """Pure planning entry used by production dispatch.

    It computes the action and seed without provider I/O or mutation of the
    live conversation; :meth:`ConversationPlan.apply` is the activation step.
    """
    return build_conversation_plan(pure=True, **kwargs)


__all__ = ["ConversationPlan", "build_conversation_plan", "plan_conversation"]
