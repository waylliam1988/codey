"""Provider-agnostic coding-agent loop state transitions."""

from __future__ import annotations

from codey.agents.context import load_project_instructions
from codey.agents.prompt_context import (
    initial_reply,
    initial_structured_reply,
    provider_supports_structured,
    send_prompt,
    send_structured_prompt,
)
from codey.agents.protocol import protocol_repair_prompt
from codey.agents.request import AgentRequest
from codey.agents.result_delivery import (
    deliver_recovered_results,
    deliver_turn_results,
)
from codey.agents.state import (
    AgentLoopSession,
    LoopProgress,
    LoopStagnation,
    LoopVerification,
    ResolvedLoopConfig,
    RunResult,
    emit,
    snapshot,
)
from codey.agents.tool_execution import (
    INFORMATION_TOOL_NAMES,
    SUPPORTED_TOOL_NAMES,
    TurnState,
    record_tool_outcome,
)
from codey.agents.tool_turn import execute_turn_tools
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.agents.verification_driver import (
    default_candidate_reminder,
    forbids_verification,
    initial_verification_state,
    requested_verification_reminder,
    requires_verification,
    selected_verification_candidate,
    verification_attempted_after_latest_edit,
    verification_is_fresh,
)
from codey.policies.permissions import profile_for_name
from codey.protocols import JsonToolCodec, ProtocolCodec
from codey.protocols.json_codec import PROTOCOL_NO_JSON
from codey.runtime.core.models import ToolPlan
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelopeSection,
)

DEFAULT_CODEC = JsonToolCodec()


class _NativeDeliveryStop(RuntimeError):
    """Internal signal: native tool chain is unrecoverable; finish as protocol."""


def parse_reply(reply: str | object, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    parse_turn = getattr(codec, "parse_turn", None)
    if not isinstance(reply, str) and callable(parse_turn):
        return parse_turn(reply)
    if isinstance(reply, str):
        return codec.parse(reply)
    # Unknown structured shape without a native codec: treat as protocol error.
    return ToolPlan(
        calls=[],
        control=None,
        protocol_error="unsupported structured reply for text codec",
        protocol_error_kind=PROTOCOL_NO_JSON,
    )


def _bounded_native_call_summary(calls: object, *, max_calls: int = 8, max_arg_chars: int = 500) -> str:
    """One bounded line per structured call: name, id (or missing), arguments."""
    import json as _json

    lines: list[str] = []
    for call in list(calls or ())[: max(1, max_calls)]:
        name = str(getattr(call, "name", "") or "?")
        call_id = str(getattr(call, "id", "") or "")
        args = getattr(call, "arguments", {})
        try:
            arg_text = _json.dumps(args, ensure_ascii=False, sort_keys=True)
        except Exception:
            arg_text = str(args)
        if len(arg_text) > max_arg_chars:
            arg_text = arg_text[:max_arg_chars].rstrip() + "..."
        lines.append(f"- {name} (id: {call_id or 'missing'}): {arg_text}")
    return "\n".join(lines)


def _reply_display_text(reply: str | object) -> str:
    if isinstance(reply, str):
        return reply
    text = str(getattr(reply, "text", "") or "")
    calls = getattr(reply, "tool_calls", ()) or ()
    if calls:
        summary = f"[tool_calls:\n{_bounded_native_call_summary(calls)}]"
        return f"{text}\n{summary}" if text else summary
    return text


def _setup_loop(request: AgentRequest) -> AgentLoopSession:
    from codey.providers.capabilities import capability_for

    provider = request.provider
    project = request.project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    profile = profile_for_name(request.permission_profile)
    codec = request.codec or JsonToolCodec(permission_profile=profile.name)
    native_tools: list[dict[str, object]] | None = None
    active_provider_hint = str(request.provider_id or getattr(provider, "name", "") or "")
    try:
        capability = capability_for(active_provider_hint)
    except Exception:
        capability = None
    wants_native = False
    if capability is not None and getattr(capability, "supports_native_tools", False):
        try:
            from codey.providers.local_config import load_local_config, resolve_local_native_tools

            wants_native = bool(resolve_local_native_tools(load_local_config()))
        except Exception:
            wants_native = bool(getattr(capability, "native_tools_default", False))
    if wants_native and callable(getattr(provider, "send_turn", None)):
        from codey.protocols.native_openai import build_native_codec_for_profile

        try:
            codec, native_tools = build_native_codec_for_profile(
                profile.name,
                fallback_codec=codec if isinstance(codec, JsonToolCodec) else None,
            )
        except Exception:
            native_tools = None
    system_prompt_text = codec.system_prompt()
    tool_fns = request.tool_fns or DEFAULT_TOOL_FNS
    max_turns = max(1, int(request.max_turns))
    stagnant_turns = max(1, int(request.stagnant_turns))
    changed_files = set(
        request.conversation.snapshot.changed_files
        if request.conversation
        else ()
    )
    changed_files.update(request.verification_changed_files)
    progress = LoopProgress(
        changed_files=changed_files,
        read_file_paths=set(),
        known_file_paths=set(),
    )
    verification = initial_verification_state(request)
    trace = FailOpenPromptTrace(request.trace_recorder)
    active_provider_id = request.provider_id or getattr(provider, "name", "")

    trace.call("record_permission_profile", profile.name, phase="writer")
    trace.call(
        "record_protocol_codec",
        str(getattr(codec, "name", "") or ""),
        phase="writer",
        model_tool_contract_hash=codec.model_tool_contract_hash(),
    )
    trace.call(
        "record_tool_contract_hash",
        codec.model_tool_contract_hash(),
        phase="writer",
    )
    trace.record_section(PromptEnvelopeSection(
        name="coding_system_prompt",
        text=system_prompt_text,
        purpose="coding JSON tool protocol",
        freshness="run_start",
        source_refs=("protocol:json",),
    ))
    trace.record_section(PromptEnvelopeSection(
        name="user_task",
        text=request.task,
        purpose="current user request",
        freshness="run_start",
        source_refs=("request:user_task",),
    ))

    project_instructions = load_project_instructions(project)
    verification.candidates = request.verification_candidates
    session = AgentLoopSession(
        request=request,
        config=ResolvedLoopConfig(
            project=project,
            codec=codec,
            profile=profile,
            tool_fns=tool_fns,
            active_provider_id=active_provider_id,
            max_turns=max_turns,
            stagnant_turns=stagnant_turns,
            system_prompt_text=system_prompt_text,
            project_instructions=tuple(project_instructions),
            native_tools=tuple(native_tools) if native_tools is not None else None,
            verification_required=requires_verification(request.task),
            verification_forbidden=forbids_verification(request.task),
        ),
        trace=trace,
        progress=progress,
        verification=verification,
        stagnation=LoopStagnation(),
    )
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        emit(session, RunEvent.info("loaded project instructions", names=names))
    return session


def _report_reply(
    session: AgentLoopSession,
    turn: int,
    reply: str | object,
    note: str = "",
) -> None:
    emit(session, RunEvent.turn_started(turn, _reply_display_text(reply), note))


def _finish(
    session: AgentLoopSession,
    summary: str,
    stop_reason: str,
    turns: int,
) -> RunResult:
    if session.request.conversation is not None:
        blocker = "" if stop_reason == "done" else summary
        session.request.conversation.update_snapshot(snapshot(session, summary, blocker))
    return RunResult(
        summary,
        stop_reason,
        turns,
        session.verification.checks_passed,
        session.progress.wrote_files,
        session.verification.checks_ran,
    )


def _use_native(session: AgentLoopSession) -> bool:
    return session.config.native_tools is not None and provider_supports_structured(session)


def _send_followup(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    include_ghost_directive: bool = True,
) -> str | object:
    if _use_native(session):
        return send_structured_prompt(session, prompt, restart_request=restart_request or prompt)
    return send_prompt(
        session, prompt, restart_request=restart_request, include_ghost_directive=include_ghost_directive,
    )


def _handle_protocol_error(
    session: AgentLoopSession,
    plan: ToolPlan,
    reply: str | object,
    turn: int,
) -> str | object | RunResult:
    session.stagnation.count += 1
    session.trace.call(
        "record_protocol_error",
        plan.protocol_error_kind,
        phase="writer",
        turn=turn,
        tool_name=str(getattr(plan, "protocol_tool_name", "") or ""),
    )
    emit(
        session,
        RunEvent.status(
            f"[agent] rejected invalid tool request: {plan.protocol_error}"
        ),
    )
    if session.stagnation.count >= session.config.stagnant_turns:
        msg = f"stopped after {session.config.stagnant_turns} invalid tool requests"
        emit(session, RunEvent.status(f"[agent] {msg}."))
        return _finish(session, msg, "protocol", turn)
    session.trace.call(
        "record_protocol_repair_prompt",
        plan.protocol_error_kind,
        phase="writer",
        turn=turn,
    )
    repair = protocol_repair_prompt(
        session.config.codec,
        plan,
        previous_reply=_reply_display_text(reply),
    )
    if _use_native(session) and not isinstance(reply, str):
        # The assistant already emitted tool_calls: answering with a plain
        # user repair prompt would leave dangling tool_call_ids (most
        # OpenAI-compatible servers reject that with a 400). Answer every
        # call id with a synthetic ERROR tool message instead, keeping the
        # chain legal, and let the model retry on the next turn.
        ids = [
            str(getattr(call, "id", "") or "")
            for call in (getattr(reply, "tool_calls", ()) or ())
        ]
        ids = [call_id for call_id in ids if call_id]
        if ids:
            from codey.agents.prompt_context import send_structured_results

            err = plan.protocol_error or "invalid native tool call"
            corrected = send_structured_results(
                session,
                [
                    {"role": "tool", "tool_call_id": call_id, "content": f"ERROR: {err}"}
                    for call_id in ids
                ],
                restart_request=repair,
            )
            _report_reply(session, turn + 1, corrected, "(after protocol correction)")
            return corrected
        if getattr(reply, "tool_calls", None):
            # tool_calls exist but none carry an answerable id: a same-chat
            # repair would dangle, so restart on a fresh chat instead. The new
            # chat gets the full project intro plus the failed calls (with
            # arguments), not just the terse repair text. If the chat cannot
            # restart, stop rather than poison the chain.
            from codey.agents.prompt_context import open_fresh_chat, project_intro, send_structured_prompt

            emit(session, RunEvent.status(
                "[agent] native turn has no answerable call id; restarting on a fresh chat."
            ))
            if open_fresh_chat(session, allow_reuse=False):
                failed = _bounded_native_call_summary(getattr(reply, "tool_calls", ()))
                request_text = (
                    f"{repair}\n\nOriginal task:\n{session.request.task}\n\n"
                    f"Failed native calls (missing ids, nothing executed):\n{failed}"
                )
                intro = project_intro(session, request_text, session.request.handoff,
                                      include_ghost_directive=False)
                if session.request.conversation is not None:
                    session.request.conversation.begin_window(
                        session.config.active_provider_id,
                        "project",
                        str(session.config.project),
                    )
                corrected = send_structured_prompt(session, intro, restart_request=request_text)
                _report_reply(session, turn + 1, corrected, "(after protocol correction)")
                return corrected
            return _finish(
                session,
                "native turn has no answerable call id and the chat cannot restart",
                "protocol",
                turn,
            )
    corrected = _send_followup(session, repair, restart_request=repair, include_ghost_directive=False)
    _report_reply(session, turn + 1, corrected, "(after protocol correction)")
    return corrected


def _deliver_turn(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
    reminder: str = "",
) -> str | object:
    from codey.protocols.native_openai import NativeToolResultError
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryError

    try:
        if reminder:
            return deliver_turn_results(session, turn_state, turn, protocol_reminder=reminder)
        return deliver_turn_results(session, turn_state, turn)
    except (NativeToolResultError, ToolResultDeliveryError) as exc:
        emit(session, RunEvent.status(f"[agent] native delivery failed: {exc}; stopping."))
        raise _NativeDeliveryStop(str(exc)) from exc


def _check_runaway_guard(session: AgentLoopSession) -> tuple[str, str]:
    from codey.agents.runaway_guard import should_block_or_remind

    try:
        guard = should_block_or_remind(session.stagnation.attempts)
    except Exception as exc:
        emit(session, RunEvent.status(f"[agent] runaway guard failed: {exc}"))
        guard = None
    guard_reason = ""
    guard_stop = ""
    if guard is not None and guard.block:
        emit(session, RunEvent.status(f"[agent] runaway guard: {guard.reason}"))
        if guard.action == "stop":
            guard_stop = guard.reason
        else:
            guard_reason = guard.reason
    return guard_reason, guard_stop


def _handle_missing_control(
    session: AgentLoopSession,
    turn_state: TurnState,
    turn: int,
    guard_reason: str,
) -> RunResult | str | object:
    if turn_state.results:
        emit(
            session,
            RunEvent.status(
                "[agent] reply had actions but no control element — returning results to model with protocol reminder."
            ),
        )
        if turn_state.made_progress:
            session.stagnation.count = 0
        else:
            session.stagnation.count += 1
            if session.stagnation.count >= session.config.stagnant_turns:
                msg = (
                    f"stopped after {session.config.stagnant_turns} turns "
                    "without file writes or new tool information"
                )
                emit(
                    session,
                    RunEvent.status(
                        f"[agent] no progress for {session.config.stagnant_turns} turns, stopping."
                    ),
                )
                return _finish(session, msg, "no_progress", turn)

        if turn >= session.config.max_turns:
            return _finish_max_turns(session, turn)

        protocol_reminder = "\n\nNote: Please remember to include a <continue> or <done> control element in your response."
        if guard_reason:
            protocol_reminder = f"{protocol_reminder}\n\n{guard_reason}"
        try:
            reply = _deliver_turn(session, turn_state, turn, protocol_reminder)
        except _NativeDeliveryStop as exc:
            return _finish(session, str(exc), "protocol", turn)
        _report_reply(session, turn + 1, reply)
        return reply

    session.stagnation.count += 1
    session.trace.call(
        "record_protocol_error",
        PROTOCOL_NO_JSON,
        phase="writer",
        turn=turn,
    )
    if session.stagnation.count >= session.config.stagnant_turns:
        msg = (
            f"stopped after {session.config.stagnant_turns} turns "
            "without valid tool progress"
        )
        emit(session, RunEvent.status(f"[agent] {msg}."))
        return _finish(session, msg, "no_progress", turn)
    emit(
        session,
        RunEvent.status(
            "[agent] reply contained no valid JSON tool call; nudging the model."
        ),
    )
    session.trace.call(
        "record_protocol_repair_prompt",
        PROTOCOL_NO_JSON,
        phase="writer",
        turn=turn,
    )
    repair = protocol_repair_prompt(
        session.config.codec,
        ToolPlan(
            calls=[],
            control=None,
            protocol_error="no JSON tool call found",
            protocol_error_kind=PROTOCOL_NO_JSON,
        ),
    )
    reply = _send_followup(session, repair, restart_request=repair, include_ghost_directive=False)
    _report_reply(session, turn + 1, reply, "(after nudge)")
    return reply


def _handle_done_control(
    session: AgentLoopSession,
    calls: object,
    control: object,
    turn: int,
) -> RunResult | str | object | None:
    needs_followup = any(call.name in INFORMATION_TOOL_NAMES for call in calls)  # type: ignore[union-attr]
    if needs_followup:
        emit(
            session,
            RunEvent.status(
                "[agent] `done` came with info action — treating as continue."
            ),
        )
        return None
    if (
        session.config.verification_required
        and session.progress.wrote_files
        and not verification_attempted_after_latest_edit(session)
    ):
        emit(
            session,
            RunEvent.status(
                "[agent] verification was requested; asking model to run a local check before done."
            ),
        )
        if turn >= session.config.max_turns:
            return _finish_max_turns(
                session, turn, "verification required before done"
            )
        reminder = requested_verification_reminder(session)
        reply = _send_followup(session, reminder, restart_request=reminder)
        _report_reply(session, turn + 1, reply, "(verification reminder)")
        return reply
    candidate = selected_verification_candidate(session)
    trusted_green = verification_is_fresh(session, candidate)
    if candidate is not None:
        session.verification.checks_passed = trusted_green
    if (
        not session.config.verification_required
        and not session.config.verification_forbidden
        and candidate is not None
        and not trusted_green
        and session.verification.default_reminded_epoch
        != session.verification.edit_epoch
    ):
        session.verification.default_reminded_epoch = (
            session.verification.edit_epoch
        )
        emit(
            session,
            RunEvent.status(
                "[agent] code changed; asking model to handle the trusted check."
            ),
        )
        if turn >= session.config.max_turns:
            return _finish_max_turns(
                session, turn, "verification did not pass"
            )
        reminder = default_candidate_reminder(candidate)
        reply = _send_followup(session, reminder, restart_request=reminder)
        _report_reply(
            session,
            turn + 1,
            reply,
            "(default verification reminder)",
        )
        return reply
    emit(session, RunEvent.status(f"[agent] DONE: {control.body}"))  # type: ignore[union-attr]
    return _finish(session, control.body, "done", turn)  # type: ignore[union-attr]


def _track_continue_progress(
    session: AgentLoopSession,
    turn_state: TurnState,
    control: object,
    turn: int,
) -> RunResult | None:
    if turn_state.made_progress:
        session.stagnation.count = 0
        return None
    session.stagnation.count += 1
    if session.stagnation.count >= session.config.stagnant_turns:
        msg = (
            control.body  # type: ignore[union-attr]
            or f"stopped after {session.config.stagnant_turns} turns without file writes or new tool information"
        )
        emit(
            session,
            RunEvent.status(
                f"[agent] no progress for {session.config.stagnant_turns} turns, stopping."
            ),
        )
        return _finish(session, msg, "no_progress", turn)
    return None


def _run_loop(
    session: AgentLoopSession,
    reply: str | object,
    *,
    start_turn: int = 1,
) -> RunResult:
    _report_reply(session, start_turn, reply)
    for turn in range(start_turn, session.config.max_turns + 1):
        if session.request.stop_flag is not None and session.request.stop_flag.is_set():

            emit(session, RunEvent.status("[agent] stopped by user."))
            return _finish(session, "stopped", "stopped", turn)
        plan = parse_reply(reply, session.config.codec)
        if plan.protocol_error:
            repaired = _handle_protocol_error(session, plan, reply, turn)
            if isinstance(repaired, RunResult):
                return repaired
            reply = repaired
            continue

        calls = plan.calls
        control = plan.control
        if calls or control is not None:
            session.trace.call(
                "record_protocol_valid_turn",
                turn,
                phase="writer",
                alias_rewrite_count=plan.alias_rewrite_count,
                arg_repair_counts=plan.arg_repair_counts,
            )

        turn_result = execute_turn_tools(session, calls, turn=turn)
        if turn_result.stopped:
            return _finish(
                session,
                turn_result.stop_summary,
                turn_result.stop_reason,
                turn,
            )
        turn_state = turn_result.turn_state
        guard_reason, guard_stop = _check_runaway_guard(session)
        if guard_stop:
            return _finish(session, guard_stop, "no_progress", turn)

        if session.request.conversation is not None:
            session.request.conversation.update_snapshot(
                snapshot(session, control.body if control else "")
            )

        if control is None:
            outcome = _handle_missing_control(session, turn_state, turn, guard_reason)
            if isinstance(outcome, RunResult):
                return outcome
            reply = outcome
            continue

        if control.kind == "done":
            done_outcome = _handle_done_control(session, calls, control, turn)
            if done_outcome is None:
                pass
            elif isinstance(done_outcome, RunResult):
                return done_outcome
            else:
                reply = done_outcome
                continue

        stagnation_stop = _track_continue_progress(session, turn_state, control, turn)
        if stagnation_stop is not None:
            return stagnation_stop

        if turn >= session.config.max_turns:
            return _finish_max_turns(session, turn, control.body or "")

        try:
            reply = _deliver_turn(session, turn_state, turn, f"\n\n{guard_reason}" if guard_reason else "")
        except _NativeDeliveryStop as exc:
            return _finish(session, str(exc), "protocol", turn)
        _report_reply(session, turn + 1, reply)

    return _finish(session, "(max turns reached)", "max_turns", session.config.max_turns)


def _finish_max_turns(session, turn: int, summary: str = "") -> RunResult:
    """Single max-turns exit so copy stays in one place."""
    body = summary or f"hit max_turns={session.config.max_turns}"
    emit(
        session,
        RunEvent.status(f"[agent] hit max_turns={session.config.max_turns}, stopping."),
    )
    return _finish(session, body, "max_turns", turn)


def run(request: AgentRequest) -> RunResult:
    session = _setup_loop(request)
    if not request.recovered_tool_outcomes:
        if _use_native(session):
            return _run_loop(session, initial_structured_reply(session), start_turn=1)
        return _run_loop(session, initial_reply(session), start_turn=1)

    turn_state = TurnState()
    recovered_outcomes = tuple(
        sorted(request.recovered_tool_outcomes, key=lambda rec: (rec.turn, rec.tool_index))
    )
    for rec in recovered_outcomes:
        record_tool_outcome(
            session,
            turn_state,
            turn=rec.turn,
            call=rec.call,
            outcome=rec.outcome,
            tool_index=rec.tool_index,
            effect_id=rec.effect_id,
            replay_class="safe",
        )

    start_turn = max((rec.turn for rec in recovered_outcomes), default=1) + 1
    if start_turn > session.config.max_turns:
        emit(
            session,
            RunEvent.status(f"[agent] hit max_turns={session.config.max_turns}, stopping."),
        )
        return _finish(
            session,
            f"hit max_turns={session.config.max_turns}",
            "max_turns",
            session.config.max_turns,
        )
    reply = deliver_recovered_results(
        session,
        turn_state,
        turn=start_turn - 1,
        recovered_batch_id=request.recovered_tool_result_batch_id,
    )
    return _run_loop(session, reply, start_turn=start_turn)

__all__ = [
    "AgentLoopSession",
    "DEFAULT_CODEC",
    "INFORMATION_TOOL_NAMES",
    "LoopProgress",
    "LoopStagnation",
    "LoopVerification",
    "RunResult",
    "SUPPORTED_TOOL_NAMES",
    "parse_reply",
    "run",
]
