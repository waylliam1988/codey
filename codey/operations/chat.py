"""Plain chat operation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from codey.agents.handoff import render_continuation_prompt, render_handoff
from codey.agents.runner import RunResult
from codey.operations.context import RunFrame
from codey.operations.prompting import (
    join_local_contexts,
    owner_prompt_with_ghost_directive,
    prepend_ghost_directive,
    record_local_context_trace,
    record_secondary_input_prepared_trace,
)
from codey.operations.result import ModeOutcome
from codey.runtime import cancellation
from codey.runtime.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt


def run_chat_mode(
    frame: RunFrame,
    *,
    state: Any,
    run_consensus: Callable[..., Any] | None,
    ghost_directive: Callable[..., Any],
    ghost_continuity: Callable[..., Any],
) -> ModeOutcome:
    request = frame.request
    if frame.provider is None:
        raise RuntimeError("provider is not connected")
    if frame.fresh_chat:
        frame.provider.new_chat()
    prompt = (
        render_continuation_prompt(frame.handoff, request.task)
        if frame.handoff
        else request.task
    )
    directive = ghost_directive(session_id=request.session_id)
    continuity = ghost_continuity(session_id=request.session_id)
    record_local_context_trace(frame.trace, directive, continuity)
    ghost_context = join_local_contexts(directive.text, continuity.text)
    prompt = prepend_ghost_directive(prompt, ghost_context)
    trace = FailOpenPromptTrace(frame.trace)
    trace.call("record_permission_profile", "chat", phase="chat")
    consulted = None
    if run_consensus is not None:
        compact_context = (
            render_handoff(frame.prior_snapshot)
            if frame.fresh_chat and frame.handoff
            else (
                render_handoff(frame.conversation.snapshot)
                if frame.conversation.initialized
                else ""
            )
        )
        try:
            owner_prompt = owner_prompt_with_ghost_directive(
                frame.recovered_owner_prompt,
                ghost_context,
            )
            record_secondary_input_prepared_trace(
                frame.trace,
                "consensus",
                task=request.task,
                context=compact_context,
                owner_prompt=owner_prompt,
            )
            consulted = run_consensus(
                selected_provider=frame.provider,
                selected_provider_id=frame.provider_id,
                task=request.task,
                context=compact_context,
                draft_first=True,
                owner_prompt=owner_prompt,
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            state.set_provider_session(frame.provider_id, None)
            raise
    if consulted is not None:
        reply = consulted.answer
    else:
        record_provider_send_prompt(
            frame.trace,
            name="chat_outbound_prompt",
            text=prompt,
            purpose="chat prompt sent to provider",
            source_ref="provider_send:chat",
            capability_id="chat_runner",
        )
        reply = frame.provider.send(prompt)
    if frame.fresh_chat:
        frame.conversation.begin_window(frame.provider_id, "chat")
    state.set_provider_session(
        frame.provider_id,
        None if consulted is not None and consulted.degraded else request.session_id,
    )
    frame.conversation.record_exchange(
        prompt,
        reply,
        replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            blocker="",
            latest_user=request.task,
            latest_reply=reply,
        ),
    )
    state.emit({
        "type": "reply",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "text": reply,
    })
    result = RunResult(reply, "done", 1)
    return ModeOutcome({
        "type": "task_done",
        "run_id": frame.run_id,
        "session_id": request.session_id,
        "summary": result.summary,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "max_turns": request.max_turns,
        "provider": frame.provider_id,
        "mode": "chat",
    })


__all__ = ["run_chat_mode"]

