"""Explicit coding-agent loop state and tool dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from codey.agents.context import (
    CODING_CURRENT_CONTEXT_BUDGET,
    ProjectInstruction,
    build_agent_context,
    load_project_instructions,
    render_completion_repair_sources,
)
from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    render_continuation_prompt,
)
from codey.agents.protocol import (
    canonical_project_path,
    default_verification_reminder,
    edit_blocks_from_call,
    edit_has_content,
    protocol_repair_prompt,
    task_forbids_verification,
    task_requests_verification,
    verification_reminder,
)
from codey.agents.request import (
    DEFAULT_MAX_TURNS,
    DEFAULT_STAGNANT_TURNS,
    AgentRequest,
    ChangeTracker,
)
from codey.agents.tools import DEFAULT_TOOL_FNS, AgentToolFns
from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY as COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
)
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_candidate,
)
from codey.policies.action import (
    DECISION_ASK_USER,
    DECISION_DENY,
    ActionPolicyDecision,
    ActionSubject,
    evaluate_action,
)
from codey.policies.permissions import allows_context_source, profile_for_name
from codey.protocols import JsonToolCodec, ProtocolCodec
from codey.protocols.json_codec import PROTOCOL_NO_JSON
from codey.runtime import cancellation
from codey.runtime.events import RunEvent
from codey.runtime.models import ToolCall, ToolPlan, ToolResult
from codey.runtime.prompt_envelope import (
    FailOpenPromptTrace,
    PromptEnvelope,
    PromptEnvelopeSection,
    RenderedPromptSection,
    record_provider_send_prompt,
)
from codey.toolchain.definition import (
    INFORMATION_RUNTIME_TOOL_NAMES,
    SUPPORTED_RUNTIME_TOOL_NAMES,
    render_tool_activity,
)
from codey.toolchain.runtime import ToolOutcome, safe_join
from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.workspace.context_epoch import context_epoch_id, context_source_ref
from codey.workspace.context_source import (
    ContextSource,
    RenderedContextSource,
    render_context_sources_with_metadata,
)

SUPPORTED_TOOL_NAMES = SUPPORTED_RUNTIME_TOOL_NAMES
INFORMATION_TOOL_NAMES = INFORMATION_RUNTIME_TOOL_NAMES
DEFAULT_CODEC = JsonToolCodec()


@dataclass
class RunResult:
    summary: str
    stop_reason: str = "done"  # done | stopped | max_turns | no_progress | protocol
    turns: int = 0
    checks_passed: bool = False
    changed: bool = False
    checks_ran: bool = False


@dataclass
class LoopProgress:
    changed_files: set[str]
    read_file_paths: set[str]
    known_file_paths: set[str]
    wrote_files: bool = False


@dataclass
class LoopVerification:
    paths: set[str]
    edit_epoch: int
    successful_checks: list[tuple[str, str, int]]
    attempts: list[tuple[str, str, int]]
    checks_ran: bool = False
    checks_passed: bool = False
    candidates_epoch: int | None = None
    default_reminded_epoch: int | None = None


@dataclass
class LoopStagnation:
    seen_info: set[tuple[str, str, str]]
    count: int = 0


@dataclass
class AgentLoopSession:
    request: AgentRequest
    provider: Any
    project: Path
    user_task: str
    codec: ProtocolCodec
    max_turns: int
    stagnant_turns: int
    on_event: Callable[[RunEvent], None]
    on_shell_request: Callable[[str, str], None] | None
    stop_flag: Any
    fresh_chat: bool
    strict_fresh_chat: bool
    change_tracker: ChangeTracker | None
    conversation: ConversationContext | None
    active_provider_id: str
    handoff: str
    project_facts: str
    research_context: str
    project_map: str
    project_config_warnings: str
    work_checkpoint: str
    verification_candidates: tuple[VerificationCandidate, ...]
    verification_candidate_loader: Callable[
        [], tuple[VerificationCandidate, ...]
    ] | None
    coding_context_enabled: bool
    ghost_directive: str
    ghost_continuity: str
    completion_repair_context: str
    completion_repair_context_payload: dict[str, object] | None
    profile: Any
    tool_fns: AgentToolFns
    trace_recorder: Any
    trace: FailOpenPromptTrace
    system_prompt_text: str
    project_text: str
    verification_required: bool
    verification_forbidden: bool
    progress: LoopProgress
    verification: LoopVerification
    stagnation: LoopStagnation
    project_instructions: list[ProjectInstruction]
    pending_context_rows: list[RenderedContextSource] = field(default_factory=list)
    pending_repair_sections: list[RenderedPromptSection] = field(default_factory=list)


@dataclass
class TurnState:
    results: list[ToolResult] = field(default_factory=list)
    made_progress: bool = False


def parse_reply(text: str, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    return codec.parse(text)


def _call_arg(call: ToolCall, name: str, default: str = "") -> str:
    value = call.args.get(name, default)
    if value is None:
        return default
    return str(value)


def _action_subject_for_call(
    call: ToolCall,
    *,
    project: Path,
    permission_profile: str,
    phase: str,
    approval_available: bool = False,
) -> ActionSubject | None:
    path = _call_arg(call, "path", ".")
    if call.name == "read":
        kind = "read_file"
    elif call.name == "ls":
        kind = "list_dir"
    elif call.name == "search":
        kind = "search_files"
    elif call.name == "references":
        kind = "find_references"
    elif call.name == "edit":
        kind = "write_file" if edit_has_content(call) else "edit_file"
    elif call.name == "run":
        kind = "run_command"
    elif call.name == "shell":
        kind = "shell"
    else:
        return None
    return ActionSubject(
        kind=kind,
        phase=phase,
        permission_profile=permission_profile,
        project=str(project),
        path=path,
        command=_call_arg(call, "command"),
        tool_name=call.name,
        approval_available=approval_available,
    )


def _policy_error_outcome(decision: ActionPolicyDecision) -> ToolOutcome:
    message = decision.display or "action denied by policy"
    text = message if message.startswith("ERROR:") else f"ERROR: {message}"
    return ToolOutcome(
        text,
        False,
        presentation={"status": "error", "result": text.removeprefix("ERROR: ")[:200]},
        audit={"error_code": "policy_denied", "policy_decision": decision.to_audit_payload()},
        error_code="policy_denied",
    )


def _read_before_edit_outcome(
    root: Path,
    rel: str,
    known_file_paths: set[str],
) -> ToolOutcome | None:
    canonical = canonical_project_path(root, rel)
    target = safe_join(root, canonical)
    if target.is_file() and canonical not in known_file_paths:
        return ToolOutcome.error(
            f"read_file required before editing existing file: {canonical}"
        )
    return None


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
    verification = LoopVerification(
        paths=set(request.verification_changed_files),
        edit_epoch=0,
        successful_checks=[
            (item.command, item.cwd, 0)
            for item in request.verification_successful_checks
        ],
        attempts=[],
    )
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
        trace.call("record_tool_contract_hash", codec.model_tool_contract_hash(), phase="writer")
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
        verification_required=task_requests_verification(request.task),
        verification_forbidden=task_forbids_verification(request.task),
        progress=progress,
        verification=verification,
        stagnation=LoopStagnation(seen_info=set()),
        project_instructions=project_instructions,
    )
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        _emit(session, RunEvent.info("loaded project instructions", names=names))
    return session


def _emit(session: AgentLoopSession, event: RunEvent) -> None:
    session.on_event(event)


def _report_reply(
    session: AgentLoopSession,
    turn: int,
    reply_text: str,
    note: str = "",
) -> None:
    _emit(session, RunEvent.turn_started(turn, reply_text, note))


def _snapshot(
    session: AgentLoopSession,
    summary: str = "",
    blocker: str = "",
) -> ConversationSnapshot:
    prior = session.conversation.snapshot if session.conversation else None
    return ConversationSnapshot(
        mode="project",
        goal=(prior.goal if prior and prior.goal else session.user_task),
        project=session.project_text,
        provider_id=session.active_provider_id,
        changed_files=tuple(sorted(session.progress.changed_files)),
        checks_passed=session.verification.checks_passed,
        summary=summary,
        blocker=blocker,
        conversation_summary=(prior.conversation_summary if prior else ""),
    )


def _finish(
    session: AgentLoopSession,
    summary: str,
    stop_reason: str,
    turns: int,
) -> RunResult:
    if session.conversation is not None:
        blocker = "" if stop_reason == "done" else summary
        session.conversation.update_snapshot(_snapshot(session, summary, blocker))
    return RunResult(
        summary,
        stop_reason,
        turns,
        session.verification.checks_passed,
        session.progress.wrote_files,
        session.verification.checks_ran,
    )


def _open_fresh_chat(session: AgentLoopSession) -> bool:
    _emit(
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
        _emit(
            session,
            RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
            ),
        )
        return False
    return True


def _with_completion_repair_context(
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


def _record_repair_context_admission(
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


def _project_intro(
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
    _record_repair_context_admission(
        session,
        epoch,
        {source.key for source in rendered.sources},
    )
    return rendered.text


def _bind_pending_context_rows(
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
    _record_repair_context_admission(session, epoch, admitted_keys)


def _discard_pending_context_rows(session: AgentLoopSession) -> None:
    session.pending_context_rows.clear()
    session.pending_repair_sections.clear()


def _send_handoff_summary(
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


def _send_prompt(
    session: AgentLoopSession,
    prompt: str,
    *,
    restart_request: str | None = None,
    include_ghost_directive: bool = True,
) -> str:
    opened_fresh_chat = False
    if session.conversation is not None and session.conversation.needs_rollover(prompt):
        factual_handoff = session.conversation.prepare_model_handoff(
            lambda summary_prompt: _send_handoff_summary(session, summary_prompt)
        )
        if _open_fresh_chat(session):
            _discard_pending_context_rows(session)
            session.trace.record_section(PromptEnvelopeSection(
                name="conversation_handoff",
                text=factual_handoff,
                purpose="bounded conversation handoff for provider rollover",
                freshness="provider_rollover",
                source_refs=("conversation:handoff",),
            ))
            prompt = _project_intro(
                session,
                restart_request or prompt,
                factual_handoff,
                include_ghost_directive=include_ghost_directive,
            )
            opened_fresh_chat = True
    if not opened_fresh_chat:
        _bind_pending_context_rows(session, prompt)
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
        session.conversation.record_exchange(prompt, reply_text, _snapshot(session))
    return reply_text


def _ensure_verification_candidates(
    session: AgentLoopSession,
) -> tuple[VerificationCandidate, ...]:
    if (
        session.verification_candidate_loader is not None
        and session.verification.paths
        and session.verification.candidates_epoch != session.verification.edit_epoch
    ):
        try:
            session.verification_candidates = session.verification_candidate_loader()
        except (OSError, TypeError, ValueError):
            session.verification_candidates = ()
        session.verification.candidates_epoch = session.verification.edit_epoch
    return session.verification_candidates


def _selected_verification_candidate(
    session: AgentLoopSession,
) -> VerificationCandidate | None:
    if not session.verification.paths:
        return None
    return select_verification_candidate(
        _ensure_verification_candidates(session),
        tuple(session.verification.paths),
    )


def _verification_is_fresh(
    session: AgentLoopSession,
    candidate: VerificationCandidate | None,
) -> bool:
    return candidate is not None and any(
        epoch == session.verification.edit_epoch
        and check_covers_selected_candidate(
            candidate,
            command,
            cwd,
            tuple(session.verification.paths),
            root=session.project,
        )
        for command, cwd, epoch in session.verification.successful_checks
    )


def _verification_attempted_after_latest_edit(session: AgentLoopSession) -> bool:
    return any(
        epoch == session.verification.edit_epoch
        for _command, _cwd, epoch in session.verification.attempts
    ) or any(
        epoch == session.verification.edit_epoch
        for _command, _cwd, epoch in session.verification.successful_checks
    )


def _current_coding_context(session: AgentLoopSession) -> str:
    if not session.coding_context_enabled:
        return ""
    candidate = _selected_verification_candidate(session)
    return render_coding_context(
        CodingContext(
            read_files=tuple(sorted(session.progress.read_file_paths)),
            edit_eligible_files=tuple(sorted(session.progress.known_file_paths)),
            changed_files=tuple(sorted(session.verification.paths)),
            selected_verification=candidate,
            verification_fresh=_verification_is_fresh(session, candidate),
            verification_forbidden=session.verification_forbidden,
        )
    )


def _append_coding_context(session: AgentLoopSession, prompt: str) -> str:
    if not allows_context_source(session.profile, "coding_current_context"):
        return prompt
    rendered_context = render_context_sources_with_metadata(
        (
            ContextSource(
                key="coding_current_context",
                loader=lambda: _current_coding_context(session),
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


def _initial_reply(session: AgentLoopSession) -> str:
    if session.fresh_chat:
        opened_fresh_chat = _open_fresh_chat(session)
        intro = _project_intro(session, session.user_task, session.handoff)
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
            session.conversation.record_exchange(intro, reply, _snapshot(session))
        return reply
    if session.conversation is not None:
        followup = (
            "Continue with the established project and JSON tool protocol.\n\n"
            f"User request:\n{session.user_task}"
        )
        return _send_prompt(
            session,
            _with_completion_repair_context(session, followup),
            restart_request=session.user_task,
        )
    intro = _project_intro(session, session.user_task)
    record_provider_send_prompt(
        session.trace_recorder,
        name="coding_outbound_prompt",
        text=intro,
        purpose="coding prompt sent to provider",
        source_ref="provider_send:coding",
        capability_id="agent_runner",
    )
    return session.provider.send(intro)


def _record_tool_outcome(
    session: AgentLoopSession,
    turn_state: TurnState,
    *,
    turn: int,
    call: ToolCall,
    outcome: ToolOutcome,
    tool_index: int,
) -> None:
    path = _call_arg(call, "path", ".")
    model_text = outcome.model_text
    _emit(session, RunEvent.tool_finished(turn, call, outcome, index=tool_index))
    turn_state.results.append(
        ToolResult(
            call=call,
            model_text=model_text,
            truncated=outcome.truncated,
            presentation=outcome.presentation,
            audit=outcome.audit,
            canonical=outcome.canonical,
        )
    )
    if call.name == "read" and outcome.ok:
        canonical = canonical_project_path(session.project, path)
        session.progress.read_file_paths.add(canonical)
        session.progress.known_file_paths.add(canonical)
    if call.name == "edit" and outcome.ok and outcome.changed:
        turn_state.made_progress = True
    produced_information = outcome.ok or outcome.exit_code is not None
    if call.name in INFORMATION_TOOL_NAMES and produced_information:
        sig = (call.name, path, model_text)
        if sig not in session.stagnation.seen_info:
            session.stagnation.seen_info.add(sig)
            turn_state.made_progress = True


def _execute_edit_call(session: AgentLoopSession, call: ToolCall) -> ToolOutcome:
    path = _call_arg(call, "path", ".")
    if edit_has_content(call):
        canonical = canonical_project_path(session.project, path)
        if safe_join(session.project, canonical).is_file():
            outcome = ToolOutcome.error(
                "content is only allowed when creating a new file; "
                f"use replacements for existing file: {canonical}"
            )
        else:
            if session.change_tracker is not None:
                session.change_tracker.capture_before(path)
            outcome = session.tool_fns.write_file(
                session.project,
                path,
                _call_arg(call, "content"),
            )
    else:
        guard = _read_before_edit_outcome(
            session.project,
            path,
            session.progress.known_file_paths,
        )
        if guard is not None:
            outcome = guard
        else:
            if session.change_tracker is not None:
                session.change_tracker.capture_before(path)
            outcome = session.tool_fns.edit_file(
                session.project,
                path,
                edit_blocks_from_call(call),
            )
    if outcome.ok and outcome.changed:
        if session.change_tracker is not None:
            session.change_tracker.capture_after(path)
        session.progress.wrote_files = True
        session.verification.checks_passed = False
        canonical = canonical_project_path(session.project, path)
        session.progress.changed_files.add(canonical)
        session.verification.paths.add(canonical)
        session.progress.known_file_paths.add(canonical)
        session.verification.edit_epoch += 1
    return outcome


def _execute_run_call(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> ToolOutcome:
    path = _call_arg(call, "path", ".")
    command = _call_arg(call, "command")
    outcome = session.tool_fns.execute_run_command(
        session.project,
        path,
        command,
        permission_profile=session.profile.name,
        phase="writer",
        tool_id=f"{turn}:{tool_index}",
    )
    session.verification.checks_ran = True
    session.verification.attempts.append(
        (command, path, session.verification.edit_epoch)
    )
    session.verification.checks_passed = outcome.ok
    if outcome.ok:
        session.verification.successful_checks.append(
            (command, path, session.verification.edit_epoch)
        )
    return outcome


def _execute_tool_call(
    session: AgentLoopSession,
    call: ToolCall,
    *,
    turn: int,
    tool_index: int,
) -> ToolOutcome:
    path = _call_arg(call, "path", ".")
    if call.name == "edit":
        return _execute_edit_call(session, call)
    if call.name == "read":
        read_options = {
            name: call.args[name]
            for name in ("offset", "limit")
            if name in call.args
        }
        return session.tool_fns.read_file(session.project, path, **read_options)
    if call.name == "ls":
        return session.tool_fns.list_directory(session.project, path)
    if call.name == "search":
        return session.tool_fns.search_files(
            session.project,
            path,
            _call_arg(call, "query"),
        )
    if call.name == "references":
        return session.tool_fns.find_references(
            session.project,
            path,
            _call_arg(call, "symbol"),
        )
    if call.name == "run":
        return _execute_run_call(session, call, turn=turn, tool_index=tool_index)
    return ToolOutcome.error(f"malformed tool call {call.name} (path={path})")


def _run_loop(session: AgentLoopSession, reply: str) -> RunResult:
    _report_reply(session, 1, reply)
    for turn in range(1, session.max_turns + 1):
        if session.stop_flag is not None and session.stop_flag.is_set():
            _emit(session, RunEvent.status("[agent] stopped by user."))
            return _finish(session, "stopped", "stopped", turn)
        plan = parse_reply(reply, session.codec)
        if plan.protocol_error:
            session.stagnation.count += 1
            session.trace.call(
                "record_protocol_error",
                plan.protocol_error_kind,
                phase="writer",
                turn=turn,
                tool_name=str(getattr(plan, "protocol_tool_name", "") or ""),
            )
            _emit(
                session,
                RunEvent.status(
                    f"[agent] rejected invalid tool request: {plan.protocol_error}"
                ),
            )
            if session.stagnation.count >= session.stagnant_turns:
                msg = f"stopped after {session.stagnant_turns} invalid tool requests"
                _emit(session, RunEvent.status(f"[agent] {msg}."))
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
            reply = _send_prompt(
                session,
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            _report_reply(session, turn + 1, reply, "(after protocol correction)")
            continue

        calls = plan.calls
        control = plan.control
        if calls or control is not None:
            session.trace.call("record_protocol_valid_turn", turn, phase="writer")

        turn_state = TurnState()
        for tool_index, call in enumerate(calls):
            path = _call_arg(call, "path", ".")
            try:
                if call.name != "shell":
                    _emit(
                        session,
                        RunEvent.tool_started(
                            turn,
                            call,
                            render_tool_activity(call),
                            index=tool_index,
                        ),
                    )
                policy_subject = _action_subject_for_call(
                    call,
                    project=session.project,
                    permission_profile=session.profile.name,
                    phase="writer",
                    approval_available=bool(session.on_shell_request),
                )
                policy_decision = (
                    evaluate_action(policy_subject)
                    if policy_subject is not None
                    else None
                )
                if policy_decision is not None:
                    session.trace.call("record_policy_decision", policy_decision)
                if (
                    policy_decision is not None
                    and policy_decision.decision == DECISION_DENY
                ):
                    outcome = _policy_error_outcome(policy_decision)
                    if call.name == "run":
                        session.verification.checks_ran = True
                        session.verification.checks_passed = False
                    _record_tool_outcome(
                        session,
                        turn_state,
                        turn=turn,
                        call=call,
                        outcome=outcome,
                        tool_index=tool_index,
                    )
                    continue
                if call.name == "shell":
                    command = _call_arg(call, "command").strip()
                    if (
                        policy_decision is not None
                        and policy_decision.decision == DECISION_ASK_USER
                        and session.on_shell_request
                    ):
                        session.on_shell_request(path, command)
                    _emit(
                        session,
                        RunEvent.status(
                            f"[agent] shell approval requested: {command}"
                        ),
                    )
                    return _finish(
                        session,
                        "shell command requires approval",
                        "approval",
                        turn,
                    )
                outcome = _execute_tool_call(
                    session,
                    call,
                    turn=turn,
                    tool_index=tool_index,
                )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception as exc:
                outcome = ToolOutcome.error(str(exc))
            _record_tool_outcome(
                session,
                turn_state,
                turn=turn,
                call=call,
                outcome=outcome,
                tool_index=tool_index,
            )

        if session.conversation is not None:
            session.conversation.update_snapshot(
                _snapshot(session, control.body if control else "")
            )

        if control is None:
            if turn_state.results:
                _emit(
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
                        _emit(
                            session,
                            RunEvent.status(
                                f"[agent] no progress for {session.stagnant_turns} turns, stopping."
                            ),
                        )
                        return _finish(session, msg, "no_progress", turn)

                if turn >= session.max_turns:
                    _emit(
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
                next_prompt = _append_coding_context(
                    session,
                    f"{formatted}{protocol_reminder}",
                )
                reply = _send_prompt(
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
                _emit(session, RunEvent.status(f"[agent] {msg}."))
                return _finish(session, msg, "no_progress", turn)
            _emit(
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
            reply = _send_prompt(
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
                _emit(
                    session,
                    RunEvent.status(
                        "[agent] `done` came with info action — treating as continue."
                    ),
                )
            elif (
                session.verification_required
                and session.progress.wrote_files
                and not _verification_attempted_after_latest_edit(session)
            ):
                _emit(
                    session,
                    RunEvent.status(
                        "[agent] verification was requested; asking model to run a local check before done."
                    ),
                )
                if turn >= session.max_turns:
                    _emit(
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
                reminder = verification_reminder(session.user_task)
                reply = _send_prompt(session, reminder, restart_request=reminder)
                _report_reply(session, turn + 1, reply, "(verification reminder)")
                continue
            else:
                candidate = _selected_verification_candidate(session)
                trusted_green = _verification_is_fresh(session, candidate)
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
                    _emit(
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
                    reminder = default_verification_reminder(candidate)
                    reply = _send_prompt(session, reminder, restart_request=reminder)
                    _report_reply(
                        session,
                        turn + 1,
                        reply,
                        "(default verification reminder)",
                    )
                    continue
                _emit(session, RunEvent.status(f"[agent] DONE: {control.body}"))
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
                _emit(
                    session,
                    RunEvent.status(
                        f"[agent] no progress for {session.stagnant_turns} turns, stopping."
                    ),
                )
                return _finish(session, msg, "no_progress", turn)

        if turn >= session.max_turns:
            _emit(
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

        next_prompt = _append_coding_context(
            session,
            session.codec.format_results(turn_state.results),
        )
        reply = _send_prompt(
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
    return _run_loop(session, _initial_reply(session))


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
