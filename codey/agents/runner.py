"""Provider-agnostic tool-using agent runtime.

Codey asks a chat provider for JSON tool calls and converts those calls into
local ToolCall objects. The runtime only depends on ChatProvider and
ProtocolCodec interfaces; browser automation and provider-specific selectors
stay outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from codey.runtime import cancellation
from codey.policies.action import (
    ActionPolicyDecision,
    ActionSubject,
    DECISION_ASK_USER,
    DECISION_DENY,
    evaluate_action,
)
from codey.workspace.coding_context import CodingContext, render_coding_context
from codey.completion.repair_context import (
    CONTEXT_SOURCE_KEY as COMPLETION_REPAIR_CONTEXT_SOURCE_KEY,
)
from codey.agents.context import (
    CODING_CURRENT_CONTEXT_BUDGET,
    build_agent_context,
    load_project_instructions,
    render_completion_repair_sources,
)
from codey.workspace.context_epoch import context_epoch_id, context_source_ref
from codey.workspace.context_source import (
    ContextSource,
    RenderedContextSource,
    render_context_sources_with_metadata,
)
from codey.runtime.events import RunEvent
from codey.agents.request import AgentRequest, DEFAULT_MAX_TURNS, DEFAULT_STAGNANT_TURNS
from codey.agents.tools import DEFAULT_TOOL_FNS
from codey.agents.handoff import (
    ConversationSnapshot,
    render_continuation_prompt,
)
from codey.runtime.models import ToolCall, ToolPlan, ToolResult
from codey.protocols import JsonToolCodec, ProtocolCodec
from codey.protocols.json_codec import (
    PROTOCOL_NO_JSON,
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
from codey.policies.permissions import allows_context_source, profile_for_name
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
from codey.toolchain.runtime import (
    ToolOutcome,
    safe_join,
)
from codey.completion.verification_policy import (
    VerificationCandidate,
    check_covers_selected_candidate,
    select_verification_candidate,
)

SUPPORTED_TOOL_NAMES = SUPPORTED_RUNTIME_TOOL_NAMES
INFORMATION_TOOL_NAMES = INFORMATION_RUNTIME_TOOL_NAMES
DEFAULT_CODEC = JsonToolCodec()


def parse_reply(text: str, codec: ProtocolCodec = DEFAULT_CODEC) -> ToolPlan:
    return codec.parse(text)


# ----------------------------------------------------------------- tools ---
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


# ----------------------------------------------------------------- loop ---

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


def run(request: AgentRequest) -> RunResult:
    provider = request.provider
    project = request.project
    user_task = request.task
    codec = request.codec
    max_turns = request.max_turns
    stagnant_turns = request.stagnant_turns
    on_event = request.on_event
    on_shell_request = request.on_shell_request
    stop_flag = request.stop_flag
    fresh_chat = request.fresh_chat
    strict_fresh_chat = request.strict_fresh_chat
    change_tracker = request.change_tracker
    conversation = request.conversation
    provider_id = request.provider_id
    handoff = request.handoff
    project_facts = request.project_facts
    research_context = request.research_context
    project_map = request.project_map
    project_config_warnings = request.project_config_warnings
    work_checkpoint = request.work_checkpoint
    verification_candidates = request.verification_candidates
    verification_candidate_loader = request.verification_candidate_loader
    verification_changed_files = request.verification_changed_files
    verification_successful_checks = request.verification_successful_checks
    coding_context_enabled = request.coding_context_enabled
    ghost_directive = request.ghost_directive
    ghost_continuity = request.ghost_continuity
    completion_repair_context = request.completion_repair_context
    completion_repair_context_payload = request.completion_repair_context_payload
    permission_profile = request.permission_profile
    tool_fns = request.tool_fns
    trace_recorder = request.trace_recorder
    project = project.resolve()
    project.mkdir(parents=True, exist_ok=True)
    profile = profile_for_name(permission_profile)
    codec = codec or JsonToolCodec(permission_profile=profile.name)
    system_prompt_text = codec.system_prompt()
    tool_fns = tool_fns or DEFAULT_TOOL_FNS
    max_turns = max(1, int(max_turns or DEFAULT_MAX_TURNS))
    stagnant_turns = max(1, int(stagnant_turns or DEFAULT_STAGNANT_TURNS))
    changed_files = set(conversation.snapshot.changed_files if conversation else ())
    changed_files.update(verification_changed_files)
    progress = LoopProgress(
        changed_files=changed_files,
        read_file_paths=set(),
        known_file_paths=set(),
    )
    verification_required = task_requests_verification(user_task)
    verification_forbidden = task_forbids_verification(user_task)
    verification = LoopVerification(
        paths=set(verification_changed_files),
        edit_epoch=0,
        successful_checks=[
            (item.command, item.cwd, 0) for item in verification_successful_checks
        ],
        attempts=[],
    )
    stagnation = LoopStagnation(seen_info=set())
    project_text = str(project)
    active_provider_id = provider_id or getattr(provider, "name", "")
    trace = FailOpenPromptTrace(trace_recorder)

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
        text=user_task,
        purpose="current user request",
        freshness="run_start",
        source_refs=("request:user_task",),
    ))

    def emit(event: RunEvent) -> None:
        on_event(event)

    def report_reply(turn: int, reply_text: str, note: str = "") -> None:
        emit(RunEvent.turn_started(turn, reply_text, note))

    def snapshot(summary: str = "", blocker: str = "") -> ConversationSnapshot:
        prior = conversation.snapshot if conversation else None
        return ConversationSnapshot(
            mode="project",
            goal=(prior.goal if prior and prior.goal else user_task),
            project=project_text,
            provider_id=active_provider_id,
            changed_files=tuple(sorted(progress.changed_files)),
            checks_passed=verification.checks_passed,
            summary=summary,
            blocker=blocker,
            conversation_summary=(prior.conversation_summary if prior else ""),
        )

    def finish(summary: str, stop_reason: str, turns: int) -> RunResult:
        if conversation is not None:
            blocker = "" if stop_reason == "done" else summary
            conversation.update_snapshot(snapshot(summary, blocker))
        return RunResult(
            summary,
            stop_reason,
            turns,
            verification.checks_passed,
            progress.wrote_files,
            verification.checks_ran,
        )

    def open_fresh_chat() -> bool:
        emit(RunEvent.status(f"[agent] opening a fresh {provider.name} conversation"))
        try:
            provider.new_chat()
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            if strict_fresh_chat:
                raise
            emit(RunEvent.status(
                f"[agent] could not open new chat: {exc}; reusing current tab"
            ))
            return False
        return True

    project_instructions = load_project_instructions(project)
    if project_instructions:
        names = ", ".join(doc.name for doc in project_instructions)
        emit(RunEvent.info("loaded project instructions", names=names))

    def with_completion_repair_context(prompt: str) -> str:
        """Attach rendered repair facts through a literal prompt envelope.

        Rows are prepared, not admitted: they bind to the outbound epoch at
        send time via bind_pending_context_rows(), because rollovers can
        still replace this prompt wholesale.
        """
        sources = render_completion_repair_sources(profile, completion_repair_context)
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
                source_refs=tuple(
                    context_source_ref(source.key) for source in sources
                ),
                budget=sum(source.budget for source in sources),
                truncated=any(source.truncated for source in sources),
                capability_id="completion_repair_context",
            ),
        )).render()
        pending_repair_sections.extend(rendered.sections)
        pending_context_rows.extend(sources)
        return rendered.text

    def record_repair_context_admission(
        epoch: str,
        admitted_keys: set[str],
    ) -> None:
        """Bind the admission row to an actual outbound provider-send epoch.

        Like the 0.4.12 continuity row: assembled is not admitted, and
        admitted is not recorded until the rendered source shared an
        outbound send boundary. The trace row proves which bytes carried
        the section -- never that the model processed them.
        """
        if not completion_repair_context_payload:
            return
        if COMPLETION_REPAIR_CONTEXT_SOURCE_KEY not in admitted_keys:
            return
        trace.call(
            "record_completion_repair_context",
            completion_repair_context_payload,
            epoch_id=epoch,
        )

    # Context-source rows prepared for a follow-up turn (for example
    # coding_current_context or completion_repair_context) are bound to
    # their provider turn only when that exact prompt is actually sent.
    # A successful rollover replaces the prompt with a fresh intro that
    # records its own rows, so stale prepared rows are discarded instead of
    # being attributed to a prompt that never leaves.
    pending_context_rows: list[RenderedContextSource] = []
    pending_repair_sections: list[RenderedPromptSection] = []

    def project_intro(
        request: str,
        factual_handoff: str = "",
        *,
        include_ghost_directive: bool = True,
    ) -> str:
        if factual_handoff:
            trace.record_section(PromptEnvelopeSection(
                name="conversation_handoff",
                text=factual_handoff,
                purpose="bounded conversation handoff for a fresh provider window",
                freshness="run_start",
                source_refs=("conversation:handoff",),
            ))
        current = (
            render_continuation_prompt(factual_handoff, request)
            if factual_handoff
            else request
        )

        rendered = build_agent_context(
            project=project,
            request_text=current,
            system_prompt_text=system_prompt_text,
            profile=profile,
            list_directory=tool_fns.list_directory,
            project_instructions=project_instructions,
            project_facts=project_facts,
            research_context=research_context,
            project_map=project_map,
            project_config_warnings=project_config_warnings,
            work_checkpoint=work_checkpoint,
            ghost_directive=ghost_directive,
            ghost_continuity=ghost_continuity,
            completion_repair_context=completion_repair_context,
            include_ghost_directive=include_ghost_directive,
        )
        # One content-addressed epoch binds every row of this turn together:
        # the assembled sections, the admitted sources, and the outbound
        # prompt recorded later through record_provider_send_prompt().
        epoch = context_epoch_id(rendered.text)
        for section in rendered.sections:
            trace.record_section(replace(section, epoch_id=epoch))
        trace.call(
            "record_context_sources",
            rendered.sources,
            epoch_id=epoch,
        )
        record_repair_context_admission(
            epoch,
            {source.key for source in rendered.sources},
        )
        return rendered.text

    def bind_pending_context_rows(prompt_text: str) -> None:
        if not pending_context_rows:
            return
        admitted_keys = {source.key for source in pending_context_rows}
        epoch = context_epoch_id(prompt_text)
        # Envelope sections first, then the source rows: same order as the
        # fresh-intro path, all bound to one content-addressed epoch.
        for section in pending_repair_sections:
            trace.record_section(replace(section, epoch_id=epoch))
        trace.call(
            "record_context_sources",
            pending_context_rows,
            epoch_id=epoch,
        )
        pending_context_rows.clear()
        pending_repair_sections.clear()
        record_repair_context_admission(epoch, admitted_keys)

    def discard_pending_context_rows() -> None:
        pending_context_rows.clear()
        pending_repair_sections.clear()

    def send_prompt(
        prompt: str,
        *,
        restart_request: str | None = None,
        include_ghost_directive: bool = True,
    ) -> str:
        opened_fresh_chat = False
        if conversation is not None and conversation.needs_rollover(prompt):
            def send_handoff_summary(summary_prompt: str) -> str:
                record_provider_send_prompt(
                    trace_recorder,
                    name="conversation_handoff_summary_prompt",
                    text=summary_prompt,
                    purpose="conversation handoff summary prompt sent to provider",
                    source_ref="provider_send:conversation_handoff_summary",
                    capability_id="conversation_handoff",
                )
                return provider.send(summary_prompt)

            factual_handoff = conversation.prepare_model_handoff(send_handoff_summary)
            if open_fresh_chat():
                discard_pending_context_rows()
                trace.record_section(PromptEnvelopeSection(
                    name="conversation_handoff",
                    text=factual_handoff,
                    purpose="bounded conversation handoff for provider rollover",
                    freshness="provider_rollover",
                    source_refs=("conversation:handoff",),
                ))
                prompt = project_intro(
                    restart_request or prompt,
                    factual_handoff,
                    include_ghost_directive=include_ghost_directive,
                )
                opened_fresh_chat = True
        if not opened_fresh_chat:
            bind_pending_context_rows(prompt)
        record_provider_send_prompt(
            trace_recorder,
            name="coding_outbound_prompt",
            text=prompt,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
        )
        reply_text = provider.send(prompt)
        if conversation is not None:
            if opened_fresh_chat:
                conversation.begin_window(active_provider_id, "project", project_text)
            conversation.record_exchange(prompt, reply_text, snapshot())
        return reply_text

    def ensure_verification_candidates() -> tuple[VerificationCandidate, ...]:
        nonlocal verification_candidates
        if (
            verification_candidate_loader is not None
            and verification.paths
            and verification.candidates_epoch != verification.edit_epoch
        ):
            try:
                verification_candidates = verification_candidate_loader()
            except (OSError, TypeError, ValueError):
                verification_candidates = ()
            verification.candidates_epoch = verification.edit_epoch
        return verification_candidates

    def selected_verification_candidate() -> VerificationCandidate | None:
        if not verification.paths:
            return None
        return select_verification_candidate(
            ensure_verification_candidates(),
            tuple(verification.paths),
        )

    def verification_is_fresh(candidate: VerificationCandidate | None) -> bool:
        return candidate is not None and any(
            epoch == verification.edit_epoch
            and check_covers_selected_candidate(
                candidate,
                command,
                cwd,
                tuple(verification.paths),
                root=project,
            )
            for command, cwd, epoch in verification.successful_checks
        )

    def verification_attempted_after_latest_edit() -> bool:
        return any(
            epoch == verification.edit_epoch for _command, _cwd, epoch in verification.attempts
        ) or any(
            epoch == verification.edit_epoch for _command, _cwd, epoch in verification.successful_checks
        )

    def current_coding_context() -> str:
        if not coding_context_enabled:
            return ""
        candidate = selected_verification_candidate()
        return render_coding_context(
            CodingContext(
                read_files=tuple(sorted(progress.read_file_paths)),
                edit_eligible_files=tuple(sorted(progress.known_file_paths)),
                changed_files=tuple(sorted(verification.paths)),
                selected_verification=candidate,
                verification_fresh=verification_is_fresh(candidate),
                verification_forbidden=verification_forbidden,
            )
        )

    def append_coding_context(prompt: str) -> str:
        if not allows_context_source(profile, "coding_current_context"):
            return prompt
        rendered_context = render_context_sources_with_metadata(
            (
                ContextSource(
                    key="coding_current_context",
                    loader=current_coding_context,
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
        # Prepared, not yet admitted: the rows bind to the outbound epoch at
        # send time (bind_pending_context_rows), because tool-result prompts
        # can still be replaced wholesale by a conversation rollover.
        pending_context_rows.extend(rendered_context.sources)
        return f"{prompt}\n\n{context}"

    if fresh_chat:
        opened_fresh_chat = open_fresh_chat()
        intro = project_intro(user_task, handoff)
        record_provider_send_prompt(
            trace_recorder,
            name="coding_outbound_prompt",
            text=intro,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
        )
        reply = provider.send(intro)
        if conversation is not None:
            if opened_fresh_chat:
                conversation.begin_window(active_provider_id, "project", project_text)
            conversation.record_exchange(intro, reply, snapshot())
    elif conversation is not None:
        followup = (
            "Continue with the established project and JSON tool protocol.\n\n"
            f"User request:\n{user_task}"
        )
        # The repair phase's first outbound prompt carries the bounded
        # failure-facts section; restart_request stays bare so a rollover
        # re-admits the section exactly once through project_intro().
        reply = send_prompt(
            with_completion_repair_context(followup),
            restart_request=user_task,
        )
    else:
        intro = project_intro(user_task)
        record_provider_send_prompt(
            trace_recorder,
            name="coding_outbound_prompt",
            text=intro,
            purpose="coding prompt sent to provider",
            source_ref="provider_send:coding",
            capability_id="agent_runner",
        )
        reply = provider.send(intro)
    report_reply(1, reply)

    for turn in range(1, max_turns + 1):
        if stop_flag is not None and stop_flag.is_set():
            emit(RunEvent.status("[agent] stopped by user."))
            return finish("stopped", "stopped", turn)
        plan = parse_reply(reply, codec)
        if plan.protocol_error:
            stagnation.count += 1
            trace.call(
                "record_protocol_error",
                plan.protocol_error_kind,
                phase="writer",
                turn=turn,
                tool_name=str(getattr(plan, "protocol_tool_name", "") or ""),
            )
            emit(RunEvent.status(
                f"[agent] rejected invalid tool request: {plan.protocol_error}"
            ))
            if stagnation.count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} invalid tool requests"
                emit(RunEvent.status(f"[agent] {msg}."))
                return finish(msg, "protocol", turn)
            # Count a repair prompt only when one actually goes out: a
            # terminal protocol failure never sends it.
            trace.call(
                "record_protocol_repair_prompt",
                plan.protocol_error_kind,
                phase="writer",
                turn=turn,
            )
            repair = protocol_repair_prompt(codec, plan, previous_reply=reply)
            reply = send_prompt(
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            report_reply(turn + 1, reply, "(after protocol correction)")
            continue
        calls = plan.calls
        control = plan.control
        if calls or control is not None:
            trace.call("record_protocol_valid_turn", turn, phase="writer")

        results: list[ToolResult] = []
        made_progress = False

        def record_tool_outcome(
            call: ToolCall,
            outcome: ToolOutcome,
            tool_index: int,
        ) -> None:
            nonlocal made_progress
            path = _call_arg(call, "path", ".")
            model_text = outcome.model_text
            emit(RunEvent.tool_finished(turn, call, outcome, index=tool_index))
            results.append(
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
                canonical = canonical_project_path(project, path)
                progress.read_file_paths.add(canonical)
                progress.known_file_paths.add(canonical)
            produced_information = outcome.ok or outcome.exit_code is not None
            if call.name in INFORMATION_TOOL_NAMES and produced_information:
                sig = (call.name, path, model_text)
                if sig not in stagnation.seen_info:
                    stagnation.seen_info.add(sig)
                    made_progress = True

        for tool_index, call in enumerate(calls):
            path = _call_arg(call, "path", ".")
            try:
                if call.name != "shell":
                    emit(RunEvent.tool_started(
                        turn,
                        call,
                        render_tool_activity(call),
                        index=tool_index,
                    ))
                policy_subject = _action_subject_for_call(
                    call,
                    project=project,
                    permission_profile=profile.name,
                    phase="writer",
                    approval_available=bool(on_shell_request),
                )
                policy_decision = (
                    evaluate_action(policy_subject)
                    if policy_subject is not None
                    else None
                )
                if policy_decision is not None:
                    trace.call("record_policy_decision", policy_decision)
                if (
                    policy_decision is not None
                    and policy_decision.decision == DECISION_DENY
                ):
                    outcome = _policy_error_outcome(policy_decision)
                    if call.name == "run":
                        verification.checks_ran = True
                        verification.checks_passed = False
                    record_tool_outcome(call, outcome, tool_index)
                    continue
                if call.name == "edit":
                    if edit_has_content(call):
                        canonical = canonical_project_path(project, path)
                        if safe_join(project, canonical).is_file():
                            outcome = ToolOutcome.error(
                                "content is only allowed when creating a new file; "
                                f"use replacements for existing file: {canonical}"
                            )
                        else:
                            if change_tracker is not None:
                                change_tracker.capture_before(path)
                            outcome = tool_fns.write_file(
                                project,
                                path,
                                _call_arg(call, "content"),
                            )
                    else:
                        guard = _read_before_edit_outcome(project, path, progress.known_file_paths)
                        if guard is not None:
                            outcome = guard
                        else:
                            if change_tracker is not None:
                                change_tracker.capture_before(path)
                            outcome = tool_fns.edit_file(
                                project,
                                path,
                                edit_blocks_from_call(call),
                            )
                    if outcome.ok and outcome.changed:
                        if change_tracker is not None:
                            change_tracker.capture_after(path)
                        made_progress = True
                        progress.wrote_files = True
                        verification.checks_passed = False
                        canonical = canonical_project_path(project, path)
                        progress.changed_files.add(canonical)
                        verification.paths.add(canonical)
                        progress.known_file_paths.add(canonical)
                        verification.edit_epoch += 1
                elif call.name == "read":
                    read_options = {
                        name: call.args[name]
                        for name in ("offset", "limit")
                        if name in call.args
                    }
                    outcome = tool_fns.read_file(project, path, **read_options)
                elif call.name == "ls":
                    outcome = tool_fns.list_directory(project, path)
                elif call.name == "search":
                    outcome = tool_fns.search_files(project, path, _call_arg(call, "query"))
                elif call.name == "references":
                    outcome = tool_fns.find_references(
                        project,
                        path,
                        _call_arg(call, "symbol"),
                    )
                elif call.name == "run":
                    command = _call_arg(call, "command")
                    outcome = tool_fns.execute_run_command(
                        project,
                        path,
                        command,
                        permission_profile=profile.name,
                        phase="writer",
                        tool_id=f"{turn}:{tool_index}",
                    )
                    verification.checks_ran = True
                    verification.attempts.append((command, path, verification.edit_epoch))
                    verification.checks_passed = outcome.ok
                    if outcome.ok:
                        verification.successful_checks.append((command, path, verification.edit_epoch))
                elif call.name == "shell":
                    command = _call_arg(call, "command").strip()
                    if (
                        policy_decision is not None
                        and policy_decision.decision == DECISION_ASK_USER
                        and on_shell_request
                    ):
                        on_shell_request(path, command)
                    emit(RunEvent.status(
                        f"[agent] shell approval requested: {command}"
                    ))
                    return finish("shell command requires approval", "approval", turn)
                else:
                    outcome = ToolOutcome.error(
                        f"malformed tool call {call.name} (path={path})"
                    )
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                # Budget exhaustion is a run-level condition, not a tool
                # error: swallowing it would burn remaining turns after the
                # provider budget is already gone.
                raise
            except Exception as exc:
                outcome = ToolOutcome.error(str(exc))
            record_tool_outcome(call, outcome, tool_index)
        if conversation is not None:
            conversation.update_snapshot(snapshot(control.body if control else ""))

        if control is None:
            if results:
                emit(RunEvent.status(
                    "[agent] reply had actions but no control element — returning results to model with protocol reminder."
                ))
                if made_progress:
                    stagnation.count = 0
                else:
                    stagnation.count += 1
                    if stagnation.count >= stagnant_turns:
                        msg = f"stopped after {stagnant_turns} turns without file writes or new tool information"
                        emit(RunEvent.status(
                            f"[agent] no progress for {stagnant_turns} turns, stopping."
                        ))
                        return finish(msg, "no_progress", turn)

                if turn >= max_turns:
                    emit(RunEvent.status(f"[agent] hit max_turns={max_turns}, stopping."))
                    return finish(f"hit max_turns={max_turns}", "max_turns", turn)

                formatted = codec.format_results(results)
                protocol_reminder = "\n\nNote: Please remember to include a <continue> or <done> control element in your response."
                next_prompt = append_coding_context(f"{formatted}{protocol_reminder}")
                reply = send_prompt(
                    next_prompt,
                    restart_request=(
                        "Continue the unfinished task using the latest local tool results below.\n\n"
                        f"{next_prompt}"
                    ),
                )
                report_reply(turn + 1, reply)
                continue

            stagnation.count += 1
            # The observation lands before the terminal check: a reply that
            # exhausts the stagnation budget is still a protocol error, so
            # error counts and real sends stay 1:1.
            trace.call(
                "record_protocol_error",
                PROTOCOL_NO_JSON,
                phase="writer",
                turn=turn,
            )
            if stagnation.count >= stagnant_turns:
                msg = f"stopped after {stagnant_turns} turns without valid tool progress"
                emit(RunEvent.status(f"[agent] {msg}."))
                return finish(msg, "no_progress", turn)
            emit(RunEvent.status(
                "[agent] reply contained no valid JSON tool call; nudging the model."
            ))
            trace.call(
                "record_protocol_repair_prompt",
                PROTOCOL_NO_JSON,
                phase="writer",
                turn=turn,
            )
            repair = protocol_repair_prompt(
                codec,
                ToolPlan(
                    calls=[],
                    control=None,
                    protocol_error="no JSON tool call found",
                    protocol_error_kind=PROTOCOL_NO_JSON,
                ),
            )
            reply = send_prompt(
                repair,
                restart_request=repair,
                include_ghost_directive=False,
            )
            report_reply(turn + 1, reply, "(after nudge)")
            continue

        if control.kind == "done":
            # Safety net: if the model said `done` but also asked for info,
            # treat it as `continue` — it almost certainly needs the result.
            needs_followup = any(call.name in INFORMATION_TOOL_NAMES for call in calls)
            if needs_followup:
                emit(RunEvent.status(
                    "[agent] `done` came with info action — treating as continue."
                ))
            elif (
                verification_required
                and progress.wrote_files
                and not verification_attempted_after_latest_edit()
            ):
                emit(RunEvent.status(
                    "[agent] verification was requested; asking model to run a local check before done."
                ))
                if turn >= max_turns:
                    emit(RunEvent.status(
                        f"[agent] hit max_turns={max_turns}, stopping."
                    ))
                    return finish("verification required before done", "max_turns", turn)
                reminder = verification_reminder(user_task)
                reply = send_prompt(reminder, restart_request=reminder)
                report_reply(turn + 1, reply, "(verification reminder)")
                continue
            else:
                candidate = selected_verification_candidate()
                trusted_green = verification_is_fresh(candidate)
                if candidate is not None:
                    verification.checks_passed = trusted_green
                if (
                    not verification_required
                    and not verification_forbidden
                    and candidate is not None
                    and not trusted_green
                    and verification.default_reminded_epoch != verification.edit_epoch
                ):
                    verification.default_reminded_epoch = verification.edit_epoch
                    emit(RunEvent.status(
                        "[agent] code changed; asking model to handle the trusted check."
                    ))
                    if turn >= max_turns:
                        return finish("verification did not pass", "max_turns", turn)
                    reminder = default_verification_reminder(candidate)
                    reply = send_prompt(reminder, restart_request=reminder)
                    report_reply(turn + 1, reply, "(default verification reminder)")
                    continue
                emit(RunEvent.status(f"[agent] DONE: {control.body}"))
                return finish(control.body, "done", turn)

        if made_progress:
            stagnation.count = 0
        else:
            stagnation.count += 1
            if stagnation.count >= stagnant_turns:
                msg = control.body or f"stopped after {stagnant_turns} turns without file writes or new tool information"
                emit(RunEvent.status(
                    f"[agent] no progress for {stagnant_turns} turns, stopping."
                ))
                return finish(msg, "no_progress", turn)

        if turn >= max_turns:
            emit(RunEvent.status(f"[agent] hit max_turns={max_turns}, stopping."))
            return finish(control.body or f"hit max_turns={max_turns}", "max_turns", turn)

        next_prompt = append_coding_context(codec.format_results(results))
        reply = send_prompt(
            next_prompt,
            restart_request=(
                "Continue the unfinished task using the latest local tool results below.\n\n"
                f"{next_prompt}"
            ),
        )
        report_reply(turn + 1, reply)

    return finish("(max turns reached)", "max_turns", max_turns)
