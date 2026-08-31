"""Prompt/context assembly and provider-send helpers for the agent loop."""

from __future__ import annotations

from dataclasses import replace

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
from codey.runtime import cancellation
from codey.runtime.events import RunEvent
from codey.runtime.prompt_envelope import (
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


def open_fresh_chat(session: AgentLoopSession) -> bool:
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
        emit(
            session,
            RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
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


def send_handoff_summary(
    session: AgentLoopSession,
    summary_prompt: str,
) -> str:
    record_provider_send_prompt(
        session.trace_recorder,
        name="conversation_handoff_summary_prompt",
        text=summary_prompt,
        purpose="conversation handoff summary prompt sent to provider",
        source_ref="provider_send:conversation_handoff_summary",
        capability_id="conversation_handoff",
    )
    return session.provider.send(summary_prompt)


def send_prompt(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    include_ghost_directive: bool = True,
) -> str:
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
    record_provider_send_prompt(
        session.trace_recorder,
        name="coding_outbound_prompt",
        text=prompt,
        purpose="coding prompt sent to provider",
        source_ref="provider_send:coding",
        capability_id="agent_runner",
    )
    reply_text = session.provider.send(prompt)
    if session.conversation is not None:
        if opened_fresh_chat:
            session.conversation.begin_window(
                session.active_provider_id,
                "project",
                session.project_text,
            )
        session.conversation.record_exchange(prompt, reply_text, snapshot(session))
    return reply_text


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
        record_provider_send_prompt(
            session.trace_recorder,
            name="coding_outbound_prompt",
            text=intro,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
        )
        reply = session.provider.send(intro)
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
    record_provider_send_prompt(
        session.trace_recorder,
        name="coding_outbound_prompt",
        text=intro,
        purpose="coding prompt sent to provider",
        source_ref="provider_send:coding",
        capability_id="agent_runner",
    )
    return session.provider.send(intro)


__all__ = [
    "append_coding_context",
    "bind_pending_context_rows",
    "current_coding_context",
    "discard_pending_context_rows",
    "initial_reply",
    "open_fresh_chat",
    "project_intro",
    "record_repair_context_admission",
    "send_handoff_summary",
    "send_prompt",
    "with_completion_repair_context",
]
