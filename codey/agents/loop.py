"""Provider-agnostic coding-agent loop state transitions."""

from __future__ import annotations

from codey.agents.context import load_project_instructions
from codey.agents.prompt_context import (
    append_coding_context,
    initial_reply,
    send_prompt,
)
from codey.agents.protocol import protocol_repair_prompt
from codey.agents.request import (
    DEFAULT_MAX_TURNS,
    DEFAULT_STAGNANT_TURNS,
    AgentRequest,
)
from codey.agents.state import (
    AgentLoopSession,
    LoopProgress,
    LoopStagnation,
    LoopVerification,
    RunResult,
    emit,
    snapshot,
)
from codey.agents.tool_execution import (
    INFORMATION_TOOL_NAMES,
    SUPPORTED_TOOL_NAMES,
    TurnState,
    call_arg,
    emit_tool_started_after_intent,
    evaluate_tool_call_policy,
    execute_tool_call,
    mark_policy_denied_run,
    policy_denied,
    policy_error_outcome,
    record_tool_call_intent,
    record_tool_call_settlement,
    record_tool_outcome,
    request_shell_approval,
    tool_error_outcome,
)
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
from codey.runtime import cancellation
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolPlan
from codey.runtime.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelopeSection,
)

DEFAULT_CODEC = JsonToolCodec()


def parse_reply(text: str, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    return codec.parse(text)


def _setup_loop(request: AgentRequest) -> AgentLoopSession:
    provider = request.provider
    project = request.project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    profile = profile_for_name(request.permission_profile)
    codec = request.codec or JsonToolCodec(permission_profile=profile.name)
    system_prompt_text = codec.system_prompt()
    tool_fns = request.tool_fns or DEFAULT_TOOL_FNS
    max_turns = max(1, int(request.max_turns or DEFAULT_MAX_TURNS))
    stagnant_turns = max(1, int(request.stagnant_turns or DEFAULT_STAGNANT_TURNS))
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
    project_text = str(project)

    trace.call("record_permission_profile", profile.name, phase="writer")
    try:
        trace.call(
            "record_protocol_codec",
            str(getattr(codec, "name", "") or ""),
            phase="writer",
            model_tool_contract_hash=codec.model_tool_contract_hash(),
        )
    except Exception:
        pass
    try:
        trace.call(
            "record_tool_contract_hash",
            codec.model_tool_contract_hash(),
            phase="writer",
        )
    except Exception:
        pass
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
    session = AgentLoopSession(
        request=request,
        provider=provider,
        project=project,
        user_task=request.task,
        codec=codec,
        max_turns=max_turns,
        stagnant_turns=stagnant_turns,
        on_event=request.on_event,
        on_shell_request=request.on_shell_request,
        stop_flag=request.stop_flag,
        fresh_chat=request.fresh_chat,
        strict_fresh_chat=request.strict_fresh_chat,
        change_tracker=request.change_tracker,
        conversation=request.conversation,
        active_provider_id=active_provider_id,
        handoff=request.handoff,
        project_facts=request.project_facts,
        research_context=request.research_context,
        project_map=request.project_map,
        project_config_warnings=request.project_config_warnings,
        work_checkpoint=request.work_checkpoint,
        verification_candidates=request.verification_candidates,
        verification_candidate_loader=request.verification_candidate_loader,
        coding_context_enabled=request.coding_context_enabled,
        ghost_directive=request.ghost_directive,
        ghost_continuity=request.ghost_continuity,
        completion_repair_context=request.completion_repair_context,
        completion_repair_context_payload=request.completion_repair_context_payload,
        profile=profile,
        tool_fns=tool_fns,
        trace_recorder=request.trace_recorder,
        trace=trace,
        system_prompt_text=system_prompt_text,
        project_text=project_text,
        verification_required=requires_verification(request.task),
        verification_forbidden=forbids_verification(request.task),
        progress=progress,
        verification=verification,
        stagnation=LoopStagnation(seen_info=set()),
        project_instructions=project_instructions,
        session_id=request.session_id,
        run_id=request.run_id,
        runtime_effects=request.runtime_effects,
    )
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        emit(session, RunEvent.info("loaded project instructions", names=names))
    return session


def _report_reply(
    session: AgentLoopSession,
    turn: int,
    reply_text: str,
    note: str = "",
) -> None:
    emit(session, RunEvent.turn_started(turn, reply_text, note))


def _finish(
    session: AgentLoopSession,
    summary: str,
    stop_reason: str,
    turns: int,
) -> RunResult:
    if session.conversation is not None:
        blocker = "" if stop_reason == "done" else summary
        session.conversation.update_snapshot(snapshot(session, summary, blocker))
    return RunResult(
        summary,
        stop_reason,
        turns,
        session.verification.checks_passed,
        session.progress.wrote_files,
        session.verification.checks_ran,
    )


def _handle_protocol_error(
    session: AgentLoopSession,
    plan: ToolPlan,
    reply: str,
    turn: int,
) -> str | RunResult:
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
    if session.stagnation.count >= session.stagnant_turns:
        msg = f"stopped after {session.stagnant_turns} invalid tool requests"
        emit(session, RunEvent.status(f"[agent] {msg}."))
        return _finish(session, msg, "protocol", turn)
    session.trace.call(
        "record_protocol_repair_prompt",
        plan.protocol_error_kind,
        phase="writer",
        turn=turn,
    )
    repair = protocol_repair_prompt(
        session.codec,
        plan,
        previous_reply=reply,
    )
    corrected = send_prompt(
        session,
        repair,
        restart_request=repair,
        include_ghost_directive=False,
    )
    _report_reply(session, turn + 1, corrected, "(after protocol correction)")
    return corrected


def _run_loop(
    session: AgentLoopSession,
    reply: str,
    *,
    start_turn: int = 1,
) -> RunResult:
    _report_reply(session, start_turn, reply)
    for turn in range(start_turn, session.max_turns + 1):
        if session.stop_flag is not None and session.stop_flag.is_set():

            emit(session, RunEvent.status("[agent] stopped by user."))
            return _finish(session, "stopped", "stopped", turn)
        plan = parse_reply(reply, session.codec)
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

        turn_state = TurnState()
        for tool_index, call in enumerate(calls):
            path = call_arg(call, "path", ".")
            effect_id = ""
            replay_decision = None
            try:
                policy_decision, replay_decision = evaluate_tool_call_policy(
                    session,
                    call,
                    turn=turn,
                    tool_index=tool_index,
                )
                if policy_denied(policy_decision):
                    assert policy_decision is not None
                    outcome = policy_error_outcome(policy_decision)
                    if call.name == "run":
                        mark_policy_denied_run(session)
                    record_tool_outcome(
                        session,
                        turn_state,
                        turn=turn,
                        call=call,
                        outcome=outcome,
                        tool_index=tool_index,
                    )
                    continue
                if call.name == "shell":
                    command = call_arg(call, "command").strip()
                    request_shell_approval(
                        session,
                        path=path,
                        command=command,
                        policy_decision=policy_decision,
                    )
                    return _finish(
                        session,
                        "shell command requires approval",
                        "approval",
                        turn,
                    )

                effect_id = record_tool_call_intent(
                    session,
                    call,
                    turn=turn,
                    tool_index=tool_index,
                    replay_decision=replay_decision,
                )
                emit_tool_started_after_intent(
                    session,
                    call,
                    turn=turn,
                    tool_index=tool_index,
                )
                try:
                    outcome = execute_tool_call(
                        session,
                        call,
                        turn=turn,
                        tool_index=tool_index,
                    )
                except (cancellation.TaskCancelled, cancellation.DeadlineExceeded) as exc:
                    if effect_id:
                        record_tool_call_settlement(
                            session,
                            effect_id,
                            outcome=tool_error_outcome(exc),
                            replay_decision=replay_decision,
                        )
                    raise
                except Exception as exc:
                    outcome = tool_error_outcome(exc)
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                outcome = tool_error_outcome(exc)

            try:
                record_tool_outcome(
                    session,
                    turn_state,
                    turn=turn,
                    call=call,
                    outcome=outcome,
                    tool_index=tool_index,
                )
            finally:
                if effect_id:
                    record_tool_call_settlement(
                        session,
                        effect_id,
                        outcome=outcome,
                        replay_decision=replay_decision,
                    )

        if session.conversation is not None:
            session.conversation.update_snapshot(
                snapshot(session, control.body if control else "")
            )

        if control is None:
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
                    if session.stagnation.count >= session.stagnant_turns:
                        msg = (
                            f"stopped after {session.stagnant_turns} turns "
                            "without file writes or new tool information"
                        )
                        emit(
                            session,
                            RunEvent.status(
                                f"[agent] no progress for {session.stagnant_turns} turns, stopping."
                            ),
                        )
                        return _finish(session, msg, "no_progress", turn)

                if turn >= session.max_turns:
                    emit(
                        session,
                        RunEvent.status(
                            f"[agent] hit max_turns={session.max_turns}, stopping."
                        ),
                    )
                    return _finish(
                        session,
                        f"hit max_turns={session.max_turns}",
                        "max_turns",
                        turn,
                    )

                formatted = session.codec.format_results(turn_state.results)
                protocol_reminder = "\n\nNote: Please remember to include a <continue> or <done> control element in your response."
                next_prompt = append_coding_context(
                    session,
                    f"{formatted}{protocol_reminder}",
                )
                reply = send_prompt(
                    session,
                    next_prompt,
                    restart_request=(
                        "Continue the unfinished task using the latest local tool results below.\n\n"
                        f"{next_prompt}"
                    ),
                )
                _report_reply(session, turn + 1, reply)
                continue

            session.stagnation.count += 1
            session.trace.call(
                "record_protocol_error",
                PROTOCOL_NO_JSON,
                phase="writer",
                turn=turn,
            )
            if session.stagnation.count >= session.stagnant_turns:
                msg = (
                    f"stopped after {session.stagnant_turns} turns "
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
                session.codec,
                ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error="no JSON tool call found",
                    protocol_error_kind=PROTOCOL_NO_JSON,
                ),
            )
            reply = send_prompt(
                session,
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            _report_reply(session, turn + 1, reply, "(after nudge)")
            continue

        if control.kind == "done":
            needs_followup = any(call.name in INFORMATION_TOOL_NAMES for call in calls)
            if needs_followup:
                emit(
                    session,
                    RunEvent.status(
                        "[agent] `done` came with info action — treating as continue."
                    ),
                )
            elif (
                session.verification_required
                and session.progress.wrote_files
                and not verification_attempted_after_latest_edit(session)
            ):
                emit(
                    session,
                    RunEvent.status(
                        "[agent] verification was requested; asking model to run a local check before done."
                    ),
                )
                if turn >= session.max_turns:
                    emit(
                        session,
                        RunEvent.status(
                            f"[agent] hit max_turns={session.max_turns}, stopping."
                        ),
                    )
                    return _finish(
                        session,
                        "verification required before done",
                        "max_turns",
                        turn,
                    )
                reminder = requested_verification_reminder(session)
                reply = send_prompt(session, reminder, restart_request=reminder)
                _report_reply(session, turn + 1, reply, "(verification reminder)")
                continue
            else:
                candidate = selected_verification_candidate(session)
                trusted_green = verification_is_fresh(session, candidate)
                if candidate is not None:
                    session.verification.checks_passed = trusted_green
                if (
                    not session.verification_required
                    and not session.verification_forbidden
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
                    if turn >= session.max_turns:
                        return _finish(
                            session,
                            "verification did not pass",
                            "max_turns",
                            turn,
                        )
                    reminder = default_candidate_reminder(candidate)
                    reply = send_prompt(session, reminder, restart_request=reminder)
                    _report_reply(
                        session,
                        turn + 1,
                        reply,
                        "(default verification reminder)",
                    )
                    continue
                emit(session, RunEvent.status(f"[agent] DONE: {control.body}"))
                return _finish(session, control.body, "done", turn)

        if turn_state.made_progress:
            session.stagnation.count = 0
        else:
            session.stagnation.count += 1
            if session.stagnation.count >= session.stagnant_turns:
                msg = (
                    control.body
                    or f"stopped after {session.stagnant_turns} turns without file writes or new tool information"
                )
                emit(
                    session,
                    RunEvent.status(
                        f"[agent] no progress for {session.stagnant_turns} turns, stopping."
                    ),
                )
                return _finish(session, msg, "no_progress", turn)

        if turn >= session.max_turns:
            emit(
                session,
                RunEvent.status(
                    f"[agent] hit max_turns={session.max_turns}, stopping."
                ),
            )
            return _finish(
                session,
                control.body or f"hit max_turns={session.max_turns}",
                "max_turns",
                turn,
            )

        next_prompt = append_coding_context(
            session,
            session.codec.format_results(turn_state.results),
        )
        reply = send_prompt(
            session,
            next_prompt,
            restart_request=(
                "Continue the unfinished task using the latest local tool results below.\n\n"
                f"{next_prompt}"
            ),
        )
        _report_reply(session, turn + 1, reply)

    return _finish(session, "(max turns reached)", "max_turns", session.max_turns)


def run(request: AgentRequest) -> RunResult:
    session = _setup_loop(request)
    if not request.recovered_tool_outcomes:
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
        )

    next_prompt = append_coding_context(
        session,
        session.codec.format_results(turn_state.results),
    )
    start_turn = max((rec.turn for rec in recovered_outcomes), default=1) + 1
    if start_turn > session.max_turns:
        emit(
            session,
            RunEvent.status(f"[agent] hit max_turns={session.max_turns}, stopping."),
        )
        return _finish(
            session,
            f"hit max_turns={session.max_turns}",
            "max_turns",
            session.max_turns,
        )
    reply = send_prompt(
        session,
        next_prompt,
        restart_request=(
            "Continue the unfinished task using the latest local tool results below.\n\n"
            f"{next_prompt}"
        ),
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
