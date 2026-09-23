"""Prompt/context assembly and provider-send helpers for the agent loop."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from codey.agents.context import (
    CODING_CURRENT_CONTEXT_BUDGET,
    build_agent_context,
    render_completion_repair_sources,
)
from codey.agents.handoff import render_continuation_prompt
from codey.agents.state import AgentLoopSession, emit, snapshot
from codey.agents.verification_driver import (
    selected_verification_candidate,
    verification_is_fresh,
)
from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY as COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
)
from codey.policies.permissions import allows_context_source
from codey.runtime.core import cancellation
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.prompt_envelope import (
    PromptEnvelope,
    PromptEnvelopeSection,
    record_provider_send_prompt,
)
from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.workspace.context_epoch import context_epoch_id, context_source_ref
from codey.workspace.context_source import (
    ContextSource,
    render_context_sources_with_metadata,
)


def open_fresh_chat(session: AgentLoopSession, *, allow_reuse: bool = True) -> bool:
    emit(
        session,
        RunEvent.status(
            f"[agent] opening a fresh {session.provider.name} conversation"
        ),
    )
    try:
        session.provider.new_chat()
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        if session.strict_fresh_chat:
            raise
        if allow_reuse:
            emit(
                session,
                RunEvent.status(
                    f"[agent] could not open new chat: {exc}; reusing current tab"
                ),
            )
        else:
            emit(
                session,
                RunEvent.status(
                    f"[agent] could not open new chat: {exc}; cannot restart, stopping"
                ),
            )
        return False
    return True


def with_completion_repair_context(
    session: AgentLoopSession,
    prompt: str,
) -> str:
    """Attach rendered repair facts through a literal prompt envelope.

    Rows are prepared, not admitted: they bind to the outbound epoch at
    send time via bind_pending_context_rows(), because rollovers can
    still replace this prompt wholesale.
    """
    sources = render_completion_repair_sources(
        session.profile,
        session.completion_repair_context,
    )
    text = "\n\n".join(source.text for source in sources)
    if not text:
        return prompt
    rendered = PromptEnvelope((
        PromptEnvelopeSection(
            name="coding_followup_request",
            text=prompt,
            purpose="continuation follow-up request",
            freshness="after_tool_result",
            source_refs=("request:user_task",),
        ),
        PromptEnvelopeSection(
            name=COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
            text=text,
            purpose="bounded failure facts from the previous completion proof",
            freshness="after_tool_result",
            source_refs=tuple(context_source_ref(source.key) for source in sources),
            budget=sum(source.budget for source in sources),
            truncated=any(source.truncated for source in sources),
            capability_id="completion_repair_context",
        ),
    )).render()
    session.pending_repair_sections.extend(rendered.sections)
    session.pending_context_rows.extend(sources)
    return rendered.text


def record_repair_context_admission(
    session: AgentLoopSession,
    epoch: str,
    admitted_keys: set[str],
) -> None:
    """Bind the admission row to an actual outbound provider-send epoch."""
    if not session.completion_repair_context_payload:
        return
    if COMPLETION_REPAIR_CONTEXT_SOURCE_KEY not in admitted_keys:
        return
    session.trace.call(
        "record_completion_repair_context",
        session.completion_repair_context_payload,
        epoch_id=epoch,
    )


def project_intro(
    session: AgentLoopSession,
    request_text: str,
    factual_handoff: str = "",
    *,
    include_ghost_directive: bool = True,
) -> str:
    if factual_handoff:
        session.trace.record_section(PromptEnvelopeSection(
            name="conversation_handoff",
            text=factual_handoff,
            purpose="bounded conversation handoff for a fresh provider window",
            freshness="run_start",
            source_refs=("conversation:handoff",),
        ))
    current = (
        render_continuation_prompt(factual_handoff, request_text)
        if factual_handoff
        else request_text
    )

    rendered = build_agent_context(
        project=session.project,
        request_text=current,
        system_prompt_text=session.system_prompt_text,
        profile=session.profile,
        list_directory=session.tool_fns.list_directory,
        project_instructions=session.project_instructions,
        project_facts=session.project_facts,
        research_context=session.research_context,
        project_map=session.project_map,
        project_config_warnings=session.project_config_warnings,
        work_checkpoint=session.work_checkpoint,
        ghost_directive=session.ghost_directive,
        ghost_continuity=session.ghost_continuity,
        completion_repair_context=session.completion_repair_context,
        include_ghost_directive=include_ghost_directive,
    )
    epoch = context_epoch_id(rendered.text)
    for section in rendered.sections:
        session.trace.record_section(replace(section, epoch_id=epoch))
    session.trace.call(
        "record_context_sources",
        rendered.sources,
        epoch_id=epoch,
    )
    record_repair_context_admission(
        session,
        epoch,
        {source.key for source in rendered.sources},
    )
    return rendered.text


def bind_pending_context_rows(
    session: AgentLoopSession,
    prompt_text: str,
) -> None:
    if not session.pending_context_rows:
        return
    admitted_keys = {source.key for source in session.pending_context_rows}
    epoch = context_epoch_id(prompt_text)
    for section in session.pending_repair_sections:
        session.trace.record_section(replace(section, epoch_id=epoch))
    session.trace.call(
        "record_context_sources",
        session.pending_context_rows,
        epoch_id=epoch,
    )
    session.pending_context_rows.clear()
    session.pending_repair_sections.clear()
    record_repair_context_admission(session, epoch, admitted_keys)


def discard_pending_context_rows(session: AgentLoopSession) -> None:
    session.pending_context_rows.clear()
    session.pending_repair_sections.clear()


def _begin_provider_send(
    session: AgentLoopSession,
    desc_text: str,
    *,
    purpose: str,
    source_ref: str,
    capability_id: str = "agent_runner",
    name: str = "coding_outbound_prompt",
    delivery_batch_id: str = "",
    supersede_effect_id: str = "",
) -> tuple[Any, str]:
    """Open a provider-send effect and bind the prompt surface to it.

    Returns ``(mutations, effect_id)``; both are empty when the run has no
    durable sinks (unit tests, ad-hoc loops). Works for text and structured
    sends alike: ``desc_text`` is the exact outbound surface (prompt text or
    serialized tool messages).
    """
    mutations = session.runtime_mutations
    effect_id = ""
    if mutations is not None and session.session_id and session.run_id:
        session.provider_send_index += 1
        from codey.runtime.core.operation_state import DRIVER_REPAIR, DRIVER_WRITER
        from codey.runtime.effects.effect_records import (
            EFFECT_CATEGORY_PROVIDER_SEND,
            RuntimeEffectIntent,
            compute_args_digest,
            new_effect_id,
        )
        from codey.runtime.effects.replay_policy import provider_replay_policy

        replay_decision = provider_replay_policy(purpose)
        effect_id = new_effect_id(EFFECT_CATEGORY_PROVIDER_SEND, session.run_id)
        driver = DRIVER_REPAIR if session.completion_repair_context else DRIVER_WRITER
        intent = RuntimeEffectIntent(
            effect_id=effect_id,
            effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
            session_id=session.session_id,
            run_id=session.run_id,
            phase=driver,
            provider_id=session.active_provider_id,
            turn=session.provider_send_index,
            display_ref=source_ref[:120],
            args_digest=compute_args_digest(desc_text),
            replay_class=replay_decision.replay_class,
        )
        committed = mutations.begin_provider_effect(
            session.session_id,
            session.run_id,
            intent,
            driver=driver,
            delivery_batch_id=delivery_batch_id,
            supersede_effect_id=supersede_effect_id,
        )
        effect_id = committed.effect_id

    # Prompt surface is bound to the exact outbound bytes and the effect that
    # carries them, so the trace row proves what was sent together.
    try:
        model_hash = session.codec.model_tool_contract_hash() if getattr(session, "codec", None) else ""
    except Exception:
        model_hash = ""
    record_provider_send_prompt(
        session.trace_recorder,
        name=name,
        text=desc_text,
        purpose=purpose,
        source_ref=source_ref,
        capability_id=capability_id,
        phase="writer",
        send_ref=effect_id,
        provider_effect_id=effect_id,
        model_tool_contract_hash=str(model_hash or ""),
        runtime_tool_contract_hash="",
    )

    from codey.runtime.hooks import call_hooks as _call_hooks

    _call_hooks(
        getattr(session, "hooks", None),
        "before_provider_send",
        session=session,
        prompt=desc_text,
        purpose=purpose,
    )
    return mutations, effect_id


def _note_failed_effect_id(exc: BaseException, effect_id: str) -> None:
    """Stash the failed provider effect id on an overflow error.

    Lets the overflow-retry site supersede exactly the attempt that failed,
    so the delivery batch records one live retry instead of conflicting.
    Internal to provider-send plumbing; never part of any wire format.
    """
    if effect_id:
        with contextlib.suppress(Exception):
            exc._codey_failed_effect_id = effect_id  # type: ignore[attr-defined]


def _failed_effect_id(exc: BaseException) -> str:
    return str(getattr(exc, "_codey_failed_effect_id", "") or "")


def _fail_provider_send(
    session: AgentLoopSession,
    mutations: Any,
    effect_id: str,
    exc: BaseException,
) -> bool:
    """Settle a failed send; True iff a NOT_SENT settlement was recorded.

    Only a recorded NOT_SENT settlement proves the attempt sent nothing
    usable, which is what allows the same delivery batch to retry. Callers
    must not supersede (or retry the batch) when this returns False.
    """
    if not (mutations is not None and effect_id):
        return False
    from codey.providers import error_classification as errors
    from codey.runtime.effects.effect_records import (
        EFFECT_CATEGORY_PROVIDER_SEND,
        SENT_STATE_MAYBE_SENT,
        SENT_STATE_NOT_SENT,
        SETTLEMENT_STATUS_ERROR,
        RuntimeEffectSettlement,
    )

    # A context-overflow rejection deterministically produced no usable
    # reply, so the attempt settles NOT_SENT (safe to supersede) instead
    # of MAYBE_SENT (must never be retried blindly). A pre-send preparation
    # failure likewise sent nothing, but its call sites never roll over:
    # only genuine overflows take the rollover retry.
    sent_state = (
        SENT_STATE_NOT_SENT
        if isinstance(exc, (errors.ContextOverflowError, errors.RequestPrepError))
        else SENT_STATE_MAYBE_SENT
    )
    try:
        mutations.settle_provider_effect(
            session.session_id,
            session.run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=session.session_id,
                run_id=session.run_id,
                status=SETTLEMENT_STATUS_ERROR,
                error_code=type(exc).__name__[:80],
                sent_state=sent_state,
            ),
        )
    except Exception:
        return False
    return sent_state == SENT_STATE_NOT_SENT


def _settle_provider_send(
    session: AgentLoopSession,
    mutations: Any,
    effect_id: str,
    *,
    prompt_desc: str,
    reply_desc: str,
    purpose: str,
) -> None:
    if mutations is not None and effect_id:
        from codey.runtime.effects.effect_records import (
            EFFECT_CATEGORY_PROVIDER_SEND,
            SENT_STATE_SETTLED,
            SETTLEMENT_STATUS_OK,
            RuntimeEffectSettlement,
        )

        mutations.settle_provider_effect(
            session.session_id,
            session.run_id,
            RuntimeEffectSettlement(
                effect_id=effect_id,
                effect_category=EFFECT_CATEGORY_PROVIDER_SEND,
                session_id=session.session_id,
                run_id=session.run_id,
                status=SETTLEMENT_STATUS_OK,
                sent_state=SENT_STATE_SETTLED,
            ),
        )

    from codey.runtime.hooks import call_hooks as _call_hooks

    _call_hooks(
        getattr(session, "hooks", None),
        "after_provider_send",
        session=session,
        prompt=prompt_desc,
        reply=reply_desc,
        purpose=purpose,
    )


def _send_provider_with_effect(
    session: AgentLoopSession,
    prompt: str,
    *,
    purpose: str,
    source_ref: str,
    capability_id: str = "agent_runner",
    name: str = "coding_outbound_prompt",
    delivery_batch_id: str = "",
    supersede_effect_id: str = "",
) -> str:
    mutations, effect_id = _begin_provider_send(
        session,
        prompt,
        purpose=purpose,
        source_ref=source_ref,
        capability_id=capability_id,
        name=name,
        delivery_batch_id=delivery_batch_id,
        supersede_effect_id=supersede_effect_id,
    )
    try:
        reply_text = session.provider.send(prompt)
    except Exception as exc:
        settled_not_sent = _fail_provider_send(session, mutations, effect_id, exc)
        from codey.providers import error_classification as errors

        if settled_not_sent and isinstance(exc, errors.ContextOverflowError):
            _note_failed_effect_id(exc, effect_id)
        raise
    _settle_provider_send(
        session,
        mutations,
        effect_id,
        prompt_desc=prompt,
        reply_desc=reply_text,
        purpose=purpose,
    )
    return reply_text


def _tool_messages_surface(tool_messages: list[dict[str, object]]) -> str:
    import json as _json

    try:
        return _json.dumps(tool_messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(tool_messages)


def _structured_send(
    session: AgentLoopSession,
    *,
    desc_text: str,
    purpose: str,
    source_ref: str,
    delivery_batch_id: str = "",
    supersede_effect_id: str = "",
    record_as: str | None = None,
    send_fn: Any,
) -> object:
    """Run one structured provider send inside the durable delivery ledger.

    Accepts an optional ``supersede_effect_id`` to void a prior attempt of
    the same batch (deterministic overflow retry): the builder records the
    void atomically with the new attempt, so the batch never shows two live
    sends.
    """
    mutations, effect_id = _begin_provider_send(
        session,
        desc_text,
        purpose=purpose,
        source_ref=source_ref,
        capability_id="agent_runner",
        name="coding_structured_prompt",
        delivery_batch_id=delivery_batch_id,
        supersede_effect_id=supersede_effect_id,
    )
    try:
        turn = send_fn()
    except Exception as exc:
        settled_not_sent = _fail_provider_send(session, mutations, effect_id, exc)
        from codey.providers import error_classification as errors

        if settled_not_sent and isinstance(exc, errors.ContextOverflowError):
            _note_failed_effect_id(exc, effect_id)
        raise
    _settle_provider_send(
        session,
        mutations,
        effect_id,
        prompt_desc=desc_text,
        reply_desc=_turn_display_text(turn),
        purpose=purpose,
    )
    if session.conversation is not None and record_as is not None:
        session.conversation.record_exchange(record_as, _turn_display_text(turn), snapshot(session))
    return turn


def send_handoff_summary(
    session: AgentLoopSession,
    summary_prompt: str,
) -> str:
    return _send_provider_with_effect(
        session,
        summary_prompt,
        purpose="conversation handoff summary prompt sent to provider",
        source_ref="provider_send:conversation_handoff_summary",
        capability_id="conversation_handoff",
        name="conversation_handoff_summary_prompt",
    )


def _rollover_for_overflow(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    include_ghost_directive: bool = True,
) -> str:
    factual_handoff = ""
    if session.conversation is not None:
        try:
            factual_handoff = session.conversation.prepare_model_handoff(
                lambda summary_prompt: send_handoff_summary(session, summary_prompt)
            )
        except Exception:
            factual_handoff = ""
    if open_fresh_chat(session):
        discard_pending_context_rows(session)
        session.trace.record_section(PromptEnvelopeSection(
            name="conversation_handoff",
            text=factual_handoff,
            purpose="bounded conversation handoff for provider overflow",
            freshness="provider_rollover",
            source_refs=("conversation:handoff",),
        ))
        prompt = project_intro(
            session,
            restart_request or prompt,
            factual_handoff,
            include_ghost_directive=include_ghost_directive,
        )
        if session.conversation is not None:
            session.conversation.begin_window(
                session.active_provider_id,
                "project",
                session.project_text,
            )
    return prompt


def send_prompt(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    include_ghost_directive: bool = True,
    delivery_batch_id: str = "",
) -> str:
    from codey.providers import error_classification as errors

    opened_fresh_chat = False
    if session.conversation is not None and session.conversation.needs_rollover(prompt):
        factual_handoff = session.conversation.prepare_model_handoff(
            lambda summary_prompt: send_handoff_summary(session, summary_prompt)
        )
        if open_fresh_chat(session):
            discard_pending_context_rows(session)
            session.trace.record_section(PromptEnvelopeSection(
                name="conversation_handoff",
                text=factual_handoff,
                purpose="bounded conversation handoff for provider rollover",
                freshness="provider_rollover",
                source_refs=("conversation:handoff",),
            ))
            prompt = project_intro(
                session,
                restart_request or prompt,
                factual_handoff,
                include_ghost_directive=include_ghost_directive,
            )
            opened_fresh_chat = True
    if not opened_fresh_chat:
        bind_pending_context_rows(session, prompt)
    try:
        reply_text = _send_provider_with_effect(
            session,
            prompt,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
            name="coding_outbound_prompt",
            delivery_batch_id=delivery_batch_id,
        )
    except errors.ContextOverflowError as exc:
        failed_effect_id = _failed_effect_id(exc)
        if delivery_batch_id and not failed_effect_id:
            # The failed attempt left no NOT_SENT proof: retrying the same
            # batch would conflict, so fail closed instead of double-sending.
            raise
        prompt = _rollover_for_overflow(
            session, prompt, restart_request=restart_request,
            include_ghost_directive=include_ghost_directive,
        )
        bind_pending_context_rows(session, prompt)
        reply_text = _send_provider_with_effect(
            session,
            prompt,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
            name="coding_outbound_prompt",
            delivery_batch_id=delivery_batch_id,
            supersede_effect_id=failed_effect_id,
        )
    if session.conversation is not None:
        if opened_fresh_chat:
            session.conversation.begin_window(
                session.active_provider_id,
                "project",
                session.project_text,
            )
        session.conversation.record_exchange(prompt, reply_text, snapshot(session))
    return reply_text


def provider_supports_structured(session: AgentLoopSession) -> bool:
    provider = getattr(session, "provider", None)
    return callable(getattr(provider, "send_turn", None)) and callable(
        getattr(provider, "send_tool_results", None)
    )


def session_native_tools(session: AgentLoopSession) -> list[dict[str, object]] | None:
    tools = getattr(session, "native_tools", None)
    if tools is not None:
        return list(tools)
    return None


def send_structured_prompt(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    delivery_batch_id: str = "",
) -> object:
    from codey.providers import error_classification as errors

    provider = session.provider
    tools = session_native_tools(session)
    bind_pending_context_rows(session, prompt)
    try:
        return _structured_send(
            session,
            desc_text=prompt,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            delivery_batch_id=delivery_batch_id,
            record_as=prompt,
            send_fn=lambda: provider.send_turn(prompt, tools, timeout=None),
        )
    except errors.ContextOverflowError as exc:
        failed_effect_id = _failed_effect_id(exc)
        if delivery_batch_id and not failed_effect_id:
            raise
        prompt = _rollover_for_overflow(session, prompt, restart_request=restart_request)
        bind_pending_context_rows(session, prompt)
        tools = session_native_tools(session)
        return _structured_send(
            session,
            desc_text=prompt,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            delivery_batch_id=delivery_batch_id,
            supersede_effect_id=failed_effect_id,
            record_as=prompt,
            send_fn=lambda: provider.send_turn(prompt, tools, timeout=None),
        )


def send_structured_results(
    session: AgentLoopSession,
    tool_messages: list[dict[str, object]],
    *,
    restart_request: str | None = None,
    delivery_batch_id: str = "",
    overflow_fallback_prompt: str = "",
    fallback_text: str | Callable[[], str] = "",
) -> object:
    """Deliver native tool results inside the durable delivery ledger.

    On context overflow the fresh chat has no preceding assistant
    ``tool_calls``, so re-sending ``role: tool`` messages would be an illegal
    chain. Instead fall back to sending the results as plain text through
    ``send_turn`` after the rollover. ``fallback_text`` may be a lazy factory
    so the normal path never pays for (or pollutes context rows with) text
    it will not send.
    """
    from codey.providers import error_classification as errors

    provider = session.provider
    tools = session_native_tools(session)
    try:
        return _structured_send(
            session,
            desc_text=_tool_messages_surface(tool_messages),
            purpose="coding tool results sent to provider",
            source_ref="provider_send:coding_tool_results",
            delivery_batch_id=delivery_batch_id,
            record_as="[tool_results]",
            send_fn=lambda: provider.send_tool_results(tool_messages, tools, timeout=None),
        )
    except errors.ContextOverflowError as exc:
        failed_effect_id = _failed_effect_id(exc)
        if delivery_batch_id and not failed_effect_id:
            # No NOT_SENT proof for the failed attempt: the batch stays
            # locked rather than risking a second live send of these results.
            raise
        resolved_fallback = fallback_text() if callable(fallback_text) else fallback_text
        text = resolved_fallback or restart_request or "[tool_results]"
        if overflow_fallback_prompt:
            text = f"{text}\n\n{overflow_fallback_prompt}"
        rolled = _rollover_for_overflow(session, text, restart_request=text)
        bind_pending_context_rows(session, rolled)
        tools = session_native_tools(session)
        return _structured_send(
            session,
            desc_text=rolled,
            purpose="coding tool results sent to provider (overflow text fallback)",
            source_ref="provider_send:coding_tool_results_overflow",
            delivery_batch_id=delivery_batch_id,
            supersede_effect_id=failed_effect_id,
            record_as=rolled,
            send_fn=lambda: provider.send_turn(rolled, tools, timeout=None),
        )


def _turn_display_text(turn: object) -> str:
    text = str(getattr(turn, "text", "") or "")
    calls = getattr(turn, "tool_calls", ()) or ()
    if calls:
        names = ", ".join(str(getattr(call, "name", "")) for call in calls)
        summary = f"[tool_calls: {names}]"
        return f"{text}\n{summary}" if text else summary
    return text


def current_coding_context(session: AgentLoopSession) -> str:
    if not session.coding_context_enabled:
        return ""
    candidate = selected_verification_candidate(session)
    return render_coding_context(
        CodingContext(
            read_files=tuple(sorted(session.progress.read_file_paths)),
            edit_eligible_files=tuple(sorted(session.progress.known_file_paths)),
            changed_files=tuple(sorted(session.verification.paths)),
            selected_verification=candidate,
            verification_fresh=verification_is_fresh(session, candidate),
            verification_forbidden=session.verification_forbidden,
        )
    )


def append_coding_context(session: AgentLoopSession, prompt: str) -> str:
    if not allows_context_source(session.profile, "coding_current_context"):
        return prompt
    rendered_context = render_context_sources_with_metadata(
        (
            ContextSource(
                key="coding_current_context",
                loader=lambda: current_coding_context(session),
                budget=CODING_CURRENT_CONTEXT_BUDGET,
                freshness="after_tool_result",
                why_included="current read, edit, and verification facts",
                capability_id="agent_runner",
                admission_reason="after_tool_result",
            ),
        )
    )
    context = rendered_context.text
    if not context:
        return prompt
    session.pending_context_rows.extend(rendered_context.sources)
    return f"{prompt}\n\n{context}"


def initial_reply(session: AgentLoopSession) -> str:
    if session.fresh_chat:
        opened_fresh_chat = open_fresh_chat(session)
        intro = project_intro(session, session.user_task, session.handoff)
        reply = _send_provider_with_effect(
            session,
            intro,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
            name="coding_outbound_prompt",
        )
        if session.conversation is not None:
            if opened_fresh_chat:
                session.conversation.begin_window(
                    session.active_provider_id,
                    "project",
                    session.project_text,
                )
            session.conversation.record_exchange(intro, reply, snapshot(session))
        return reply
    if session.conversation is not None:
        followup = (
            "Continue with the established project and JSON tool protocol.\n\n"
            f"User request:\n{session.user_task}"
        )
        return send_prompt(
            session,
            with_completion_repair_context(session, followup),
            restart_request=session.user_task,
        )
    intro = project_intro(session, session.user_task)
    return _send_provider_with_effect(
        session,
        intro,
        purpose="coding prompt sent to provider",
        source_ref="provider_send:coding",
        capability_id="agent_runner",
        name="coding_outbound_prompt",
    )


def initial_structured_reply(session: AgentLoopSession) -> object:
    if session.fresh_chat:
        opened_fresh_chat = open_fresh_chat(session)
        intro = project_intro(session, session.user_task, session.handoff)
        turn = send_structured_prompt(session, intro, restart_request=session.user_task)
        if session.conversation is not None and opened_fresh_chat:
            session.conversation.begin_window(
                session.active_provider_id,
                "project",
                session.project_text,
            )
        return turn
    if session.conversation is not None:
        followup = (
            "Continue with the established project and tool protocol.\n\n"
            f"User request:\n{session.user_task}"
        )
        return send_structured_prompt(
            session,
            with_completion_repair_context(session, followup),
            restart_request=session.user_task,
        )
    intro = project_intro(session, session.user_task)
    return send_structured_prompt(session, intro, restart_request=session.user_task)


__all__ = [
    "append_coding_context",
    "bind_pending_context_rows",
    "current_coding_context",
    "discard_pending_context_rows",
    "initial_reply",
    "initial_structured_reply",
    "open_fresh_chat",
    "project_intro",
    "provider_supports_structured",
    "record_repair_context_admission",
    "send_handoff_summary",
    "send_prompt",
    "send_structured_prompt",
    "send_structured_results",
    "session_native_tools",
    "with_completion_repair_context",
]
