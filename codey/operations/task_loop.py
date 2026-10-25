"""One task tool loop for every task kind (operations layer).

Production topology: policy -> session -> snapshot -> adapter.send ->
normalize_turn -> done gate or execute_turn -> record -> deliver. Web text
JSON and native tool calls converge in ``kernel_protocol.normalize_turn``;
facts live in ``task_session.TaskSession``; project, web, and knowledge
tools dispatch through ``execute_turn``; ``done`` converges in the single
``completion_gate``. The loop sends exactly one provider message per
iteration (no double-send): a ``done`` rejection becomes the next prompt,
tool results become the next prompt (web) or the next tool_results call
(native) returning the following reply.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from codey.operations import kernel_events as _events
from codey.operations import kernel_prompt as _prompt
from codey.operations import kernel_protocol
from codey.operations import kernel_transport as _transport
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_execution import execute_turn as _execute_turn
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_progress import KernelProgress
from codey.operations.kernel_protocol import InitialNativeTurn
from codey.operations.kernel_protocol import normalize_turn as _normalize_turn
from codey.operations.kernel_recovery import apply_recovery_first as _apply_recovery_first
from codey.operations.kernel_trace import record_outbound, record_turn
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import effect_coordinates
from codey.operations.task_session import turn_effect_id as _turn_effect_id
from codey.policies.command_line import ShellSpec
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import ProviderToolDefinition, ProviderToolResult, TurnFinish, tools_from_specs
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.workspace.coding_context import render_coding_context
from codey.workspace.context_admission import GhostContextSource, admit_ghost_context
from codey.workspace.context_epoch import ContextEpoch, context_epoch_id

if TYPE_CHECKING:  # Annotations only; the loop never re-exports TaskSession.
    from codey.operations.task_session import TaskSession

__all__ = [
    "KernelResult",
    "KernelExecutionDeps",
    "KernelObservationDeps",
    "KernelRunRequest",
    "KernelTransportDeps",
    "run_task_kernel",
]


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str
    proof: Any | None = None
    delivery: str = "not_required"

@dataclass(frozen=True)
class KernelTransportDeps:
    """Provider and turn transport state owned by the kernel boundary."""

    provider: Any
    run_id: str = ""
    effect_scope: str = ""
    provider_id: object = ""
    user_task: object = ""
    context_text: str = ""
    # Logical request authority, independent of an automatically allocated
    # execution workspace. Unscoped standalone requests use "request".
    context_scope: str = "request"
    ghost_sources: tuple[GhostContextSource, ...] = ()
    admit_hints: bool = True
    attempt_gate: Callable[[Any, dict[str, Any]], None] | None = None
    attempt_admit_hints: Callable[[], bool] | None = None
    stop_flag: Any = None
    stagnant_turns: int | None = None
    delivered: Mapping[str, ToolResult] | None = None
    start_turn: int | None = None
    initial_results: list[ToolResult] | None = None
    provider_session_changed: bool = False
    initial_turn: InitialNativeTurn | None = None
    window_ref: Any = None
    replayed_result_ids: frozenset[str] = frozenset()

@dataclass(frozen=True)
class KernelExecutionDeps:
    """Tool, project, and workspace resources for one kernel run."""

    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None
    project_path: Any = None
    tool_fns: Any = None
    research_tools: Any = None
    change_tracker: Any = None
    managed_outputs: Any = None
    session_id: str = ""
    permission_profile: str = "coding_writer"
    workspace_ignored_paths: Any = ()
    workspace_revision_store: Any = None
    ensure_workspace_writer: Callable[[], None] | None = None
    durable: Any = None

@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    intent_sink: Any = None
    completion_context: Any = None
    on_event: Callable[[Any], None] | None = None
    on_shell_request: Callable[[Any], None] | None = None
    trace_recorder: Any = None
    propagate_provider_failure: bool = False
    # Called after a completed tool round or before final task handoff.  The
    # callback owns durable inbox placement; its returned text is appended to
    # the next text-provider prompt.  Native providers keep their protocol
    # receipt chain intact and therefore consume the durable placement on the
    # following generation boundary.
    on_boundary: Callable[[str], str | None] | None = None

@dataclass(frozen=True)
class KernelRunRequest:
    """Single dependency boundary for the shared task kernel."""

    transport: KernelTransportDeps
    execution: KernelExecutionDeps = KernelExecutionDeps()
    observation: KernelObservationDeps = KernelObservationDeps()
    task_guidance: str = ""


@dataclass
class _ProviderTurnState:
    """Pending transport slots and bounded reply repair counters."""

    prompt: str
    pending_reply: Any = None
    pending_messages: list[ProviderToolResult] | None = None
    invalid_turns: int = 0
    length_continuation_used: bool = False


@dataclass(frozen=True)
class _ReceivedPlan:
    plan: ToolPlan
    reply: Any


@dataclass(frozen=True)
class _KernelStartup:
    executors: dict[str, Any]
    delivered: dict[str, ToolResult]
    max_turns: int
    native: bool
    identity_ref: str
    state: _ProviderTurnState
    native_tools: list[ProviderToolDefinition]
    snapshot: Any
    pending_recovery: bool
    attempt_gate_bound: bool = False
    window_ref: Any = None


def _payload_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for item in value.values():
            result.extend(_payload_strings(item))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for item in value:
            result.extend(_payload_strings(item))
        return result
    return []


def _bind_attempt_gate(
    provider: Any, transport: KernelTransportDeps, trace_recorder: Any,
) -> bool:
    binder = getattr(provider, "bind_attempt_gate", None)
    if not callable(binder):
        return False
    gate: Callable[[Any, dict[str, Any]], None] | None = transport.attempt_gate
    if gate is None and transport.ghost_sources:
        def gate(client: Any, payload: dict[str, Any]) -> None:
            rendered = "\n".join(_payload_strings(payload))
            enabled = (transport.attempt_admit_hints() if transport.attempt_admit_hints is not None
                       else transport.admit_hints)
            sources = transport.ghost_sources
            if getattr(client, "purpose", "conversation") == "compaction":
                # Auxiliary prompts may contain only a declared encoding of a
                # source.  Gate the representations that are actually present;
                # a missing source is not invented as a failure.
                variants = []
                for source in sources:
                    escaped = replace(source, text=json.dumps(source.text, ensure_ascii=False)[1:-1])
                    variants.append((source, escaped))
                present = tuple(escaped if escaped.text in rendered else source
                                for source, escaped in variants
                                if source.text in rendered or escaped.text in rendered)
                sources = present
            admission = admit_ghost_context(
                rendered, sources, request_scope=transport.context_scope,
                require_all=bool(sources), admit_hints=enabled,
            )
            if getattr(client, "purpose", "conversation") == "conversation":
                prompt = getattr(client, "_attempt_gate_prompt", "") or rendered
                admission = replace(
                    admission,
                    epoch=ContextEpoch(epoch_id=context_epoch_id(prompt), admissions=admission.epoch.admissions),
                )
                record_outbound(trace_recorder, prompt, admission=admission)
    binder(gate)
    return gate is not None


def _receive_turn_plan(
    session: TaskSession, provider: Any, state: _ProviderTurnState, snapshot: Any, native_tools: Any,
    *, native: bool, provider_id: object, turn: int, stop_flag: Any, on_event: Any, window_ref: Any = None,
    stagnant_turns: int | None, propagate_provider_failure: bool, trace_recorder: Any, context_scope: str,
    ghost_sources: tuple[GhostContextSource, ...] = (), require_ghost_sources: bool = False,
    admit_hints: bool = True,
    kernel_admission: bool = True,
) -> _ReceivedPlan | KernelResult | None:
    """Receive once; cancellation, truncation and protocol repair precede execution.

    None requests the next turn using the updated transport state. This helper
    never loops, executes tools, or accepts completion.
    """
    try:
        if state.pending_reply is None:
            refs = getattr(provider, "set_result_refs", None)
            if callable(refs):
                refs(tuple(session._memory_results))
        set_gate_prompt = getattr(provider, "set_attempt_gate_prompt", None)
        if callable(set_gate_prompt):
            set_gate_prompt(state.prompt)
        if kernel_admission and state.pending_reply is None and not (native and state.pending_messages is not None):
            admission = admit_ghost_context(
                state.prompt, ghost_sources, request_scope=context_scope,
                require_all=require_ghost_sources, admit_hints=admit_hints,
            )
            record_outbound(trace_recorder, state.prompt, admission=admission)
        reply, state.pending_reply, state.pending_messages = _transport.send_kernel_reply(
            provider, native, state.prompt, native_tools, state.pending_reply, state.pending_messages,
            window_ref, observations=_prompt.working_context(session),
        )
    except Exception as exc:
        return _provider_failure(exc, turn, propagate=propagate_provider_failure)
    cancelled = _cancel_after_send(stop_flag, provider, reply, native, turn,
                                  propagate=propagate_provider_failure, declared_tools=native_tools)
    if cancelled is not None:
        return cast(_ReceivedPlan | KernelResult | None, cancelled)
    _events._emit_turn_event(on_event, turn, reply)
    length_action = _length_reply_action(
        reply, native=native, used=state.length_continuation_used, turns=turn,
    )
    if isinstance(length_action, KernelResult):
        return length_action
    if length_action is not None:
        state.length_continuation_used = True
        state.prompt = length_action
        state.pending_reply = None
        return None
    decoded_reply = decode_text_turn(reply) if not native and isinstance(reply, str) else reply
    plan = _normalize_turn(decoded_reply, snapshot=snapshot)
    record_turn(trace_recorder, phase=session.task_kind, turn=turn,
                snapshot=snapshot, plan=plan, native=native)
    protocol_action = _kernel_handle_protocol(
        plan, provider, reply, native, native_tools, stagnant_turns=stagnant_turns,
        invalid_turns=state.invalid_turns, turn=turn, contract_text=snapshot.contract_text,
    )
    if isinstance(protocol_action, KernelResult):
        return protocol_action
    if protocol_action is not None:
        state.prompt, state.pending_reply, state.invalid_turns = protocol_action
        return None
    state.invalid_turns = 0
    return _ReceivedPlan(plan, reply)


def _length_reply_action(
    reply: Any, *, native: bool, used: bool, turns: int,
) -> KernelResult | str | None:
    if not native:
        return None
    if getattr(reply, "finish", None) is not TurnFinish.OUTPUT_LIMIT:
        return None
    if used:
        return KernelResult(
            completed=False,
            summary="local provider emitted finish_reason=length twice",
            turns=turns,
            stop_reason="provider_failure",
        )
    if getattr(reply, "tool_calls", ()):
        return None
    return (
        "Your previous response was truncated before a tool call. "
        "Continue the task with the next required tool call, or call "
        "done(summary) if the verified task is complete. Do not output "
        "ordinary explanation."
    )


def _provider_failure(exc: Exception, turns_used: int, *, propagate: bool) -> KernelResult:
    from codey.durable.errors import CommitConflict
    from codey.runtime.core.cancellation import TaskCancelled

    cancellation_barrier = isinstance(exc, CommitConflict) and str(exc) == "task cancellation barrier is active"
    if isinstance(exc, TaskCancelled) or cancellation_barrier:
        return KernelResult(completed=False, summary="stopped", turns=turns_used, stop_reason="stopped")
    if propagate:
        raise exc
    # Preserve the error type for invariant diagnostics: a bare str(exc)
    # would hide whether the fault was a settle invariant, a digest outage,
    # or a genuine provider transport error.
    try:
        kind = type(exc).__name__ or "Exception"
    except Exception:
        kind = "Exception"
    return KernelResult(
        completed=False,
        summary=f"provider failed [{kind}]: {exc}",
        turns=turns_used,
        stop_reason="provider_failure",
    )


def _approval_stop(
    calls: list[ToolCall],
    *,
    project_path: Any,
    on_shell_request: Callable[[Any], None] | None,
    turn: int,
    run_id: object = "",
    intent_sink: Any = None,
    policy: Any = None,
    permission_profile: object = "coding_writer",
    provider_id: object = "",
    window_ref: Any = None,
    shell: ShellSpec | None = None,
    durable: Any = None,
    managed_outputs: Any = None,
    task_id: str = "",
) -> KernelResult | None:
    if on_shell_request is None or project_path is None:
        return None
    from codey.agents.shell_approval import ShellApprovalRequest, deferred_tool_call_from_call
    from codey.operations.task_execution import effective_project_profile
    from codey.policies.command_approval import analyze_approval_call
    from codey.policies.command_line import resolve_shell

    try:
        allows_shell = bool(policy.allows("shell.approval")) if policy is not None else False
    except Exception:
        allows_shell = False
    if not allows_shell:
        # No TaskPolicy grant: read-only and unauthorized Research never
        # pause here. Execution still denies via the project guard.
        return None
    effective_profile = effective_project_profile(permission_profile)
    selected_shell = shell if shell is not None else resolve_shell()
    for index, call in enumerate(calls):
        from codey.operations.shell_authorization import denied_shell_message

        if denied_shell_message(call, durable=durable, managed_outputs=managed_outputs,
                                run_id=task_id or str(run_id), project=project_path, shell=selected_shell):
            # Normal execution settles and delivers the refusal to this call.
            continue
        if call.name == "run" and len(calls) > 1:
            # Do not park a whole batch and silently skip earlier effects.
            # Normal execution returns a refusal for this command; the model
            # can submit it alone without replaying successful siblings.
            continue
        if call.name == "run" and not policy.allows("project.verify"):
            continue
        analysis = analyze_approval_call(call, project=project_path, permission_profile=effective_profile, shell=selected_shell)
        if analysis is None:
            continue
        deferred = tuple(
            deferred_tool_call_from_call(item, tool_index=offset)
            for offset, item in enumerate(calls[index + 1 :], start=index + 1)
        )
        if intent_sink is not None:
            intent_sink.begin_turn(
                [
                    (_turn_effect_id(str(run_id or "adhoc"), turn, offset), item, offset)
                    for offset, item in enumerate(calls[: index + 1])
                ],
                turn=turn,
            )
        on_shell_request(
            ShellApprovalRequest(
                cwd=str(call.args.get("path") or "."),
                command=str(call.args.get("command") or ""),
                deferred_calls=deferred,
                call_id=str(getattr(call, "call_id", "") or ""),
                provider_id=str(provider_id or ""),
                turn=turn,
                tool_index=index,
                window_ref=window_ref,
                tool_name=call.name,
                tool_args=dict(call.args),
                shell=selected_shell,
                analysis=analysis,
            )
        )
        return KernelResult(False, "shell command requires approval", turn, "approval")
    return None


def _stop_requested(stop_flag: Any) -> bool:
    if stop_flag is None:
        return False
    try:
        return bool(stop_flag.is_set())
    except Exception:
        return False


def _kernel_handle_protocol(
    plan: Any,
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    *,
    stagnant_turns: int | None,
    invalid_turns: int,
    turn: int,
    contract_text: str,
) -> Any:
    """Handle one protocol error; None means proceed to tools."""
    if not getattr(plan, "protocol_error", ""):
        return None
    invalid_turns += 1
    if stagnant_turns is not None and invalid_turns >= max(1, stagnant_turns):
        # 阈值终止走同一有界关闭：回答当前 id 及后续新调用，不执行工具。
        # 关闭失败必须反馈 provider failure。
        if native and not isinstance(reply, str):
            try:
                _transport.close_native_reply(
                    provider, reply,
                    str(getattr(plan, "protocol_error", "") or "protocol error"),
                    declared_tools=native_tools,
                )
            except Exception as exc:
                return KernelResult(
                    completed=False, summary=f"provider failed: {exc}",
                    turns=turn, stop_reason="provider_failure",
                )
        return KernelResult(
            False, f"stopped after {invalid_turns} invalid tool requests: {plan.protocol_error}", turn, "protocol"
        )
    prompt = _prompt._repair_prompt(plan.protocol_error, contract_text=contract_text, native=native)
    try:
        pending_reply = _transport.repair_native_dangling(
            provider, reply, native, native_tools, plan.protocol_error, instruction=prompt)
    except Exception as exc:
        # The error receipt never reached the provider: the native chain has
        # unanswered call ids, so stop instead of continuing the dialogue.
        return KernelResult(
            completed=False, summary=f"provider failed: {exc}", turns=turn, stop_reason="provider_failure"
        )
    return (prompt, pending_reply, invalid_turns)


def _kernel_handle_done_approval(
    session: Any,
    plan: Any,
    calls: list[Any],
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    *,
    completion_context: Any,
    project_path: Any,
    on_shell_request: Any,
    turn: int,
    identity_ref: str,
    intent_sink: Any,
    permission_profile: object = "coding_writer",
    provider_id: object = "",
    on_event: Any = None,
    durable: Any = None,
    managed_outputs: Any = None,
    run_id: str = "",
) -> tuple[Any, Any, list[Any]]:
    """Handle done/approval; returns (done_action, approval, calls).

    done_action is None (proceed), KernelResult (return), or tuple (prompt, pending) to continue.
    approval is None (proceed) or KernelResult (return).
    """
    done_action = None
    if getattr(getattr(plan, "control", None), "kind", "") == "ask_user":
        from codey.operations.kernel_question import pause_for_question
        try:
            question = pause_for_question(dict(plan.control_args), provider, reply, native, native_tools,
                on_event=on_event, identity=identity_ref, turn=turn)
            return KernelResult(completed=False, summary=question, turns=turn, stop_reason="question"), None, []
        except Exception as exc:
            return KernelResult(completed=False, summary=f"question delivery failed: {exc}", turns=turn, stop_reason="delivery_pending"), None, []
    if getattr(getattr(plan, "control", None), "kind", "") == "done":
        done_outcome = _handle_done_reply(
            session,
            plan,
            provider,
            reply,
            native,
            native_tools,
            completion_context=completion_context,
        )
        if done_outcome is not None:
            done_action = done_outcome
    approval = _approval_stop(
        calls,
        project_path=project_path,
        on_shell_request=on_shell_request,
        turn=turn,
        run_id=identity_ref,
        intent_sink=intent_sink,
        policy=getattr(session, "policy", None),
        permission_profile=permission_profile,
        provider_id=provider_id,
        window_ref=getattr(provider, "window_ref", None),
        shell=session.shell,
        durable=durable,
        managed_outputs=managed_outputs,
        task_id=run_id,
    )
    return done_action, approval, list(calls or ())


def _outer_evidence_for_context(completion_context: Any) -> Any:
    try:
        if isinstance(completion_context, dict):
            return completion_context.get("execution_evidence")
        return getattr(completion_context, "execution_evidence", None)
    except Exception:
        return None


def _kernel_startup(
    session: TaskSession,
    provider: Any,
    *,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None,
    run_id: str,
    effect_scope: str,
    provider_id: object,
    user_task: object,
    context_text: str,
    task_guidance: str,
    completion_context: Any,
    initial_results: list[ToolResult] | None,
    provider_session_changed: bool,
    delivered: Mapping[str, ToolResult] | None,
    initial_turn: InitialNativeTurn | None = None,
    replayed_result_ids: frozenset[str] = frozenset(),
    attempt_gate_bound: bool = False,
) -> _KernelStartup:
    runnable = dict(executors or {})
    delivered_map = dict(delivered or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    pending_initial = list(initial_results or [])
    active_window = getattr(provider, "window_ref", None)
    recovered_windows = {
        row_window
        for row in pending_initial
        if (row_window := getattr(row, "window_ref", None)) is not None
    }
    if len(recovered_windows) > 1:
        raise ValueError("recovered results belong to multiple protocol windows")
    recovered_window = next(iter(recovered_windows), None)
    for recovered in pending_initial:
        row_window = getattr(recovered, "window_ref", None)
        if row_window is not None and row_window != active_window:
            raise ValueError("shell result belongs to another protocol window")
    native = _transport.provider_uses_native(provider, provider_id=provider_id)
    identity_ref = f"{run_id}:{effect_scope}" if effect_scope else run_id
    if initial_turn is not None and (not native or pending_initial or initial_turn.owner_session is not session
                                     or initial_turn.snapshot.policy != session.policy):
        raise ValueError("initial native turn does not match the authorized task session")
    if initial_turn is not None:
        initial_snapshot, prompt = initial_turn.snapshot, initial_turn.prompt
    else:
        prepared = prepare_kernel_turn(
            session, user_task=str(user_task or ""), context_text=context_text,
            native=native, task_guidance=task_guidance, completion_context=completion_context,
        )
        initial_snapshot, prompt = prepared.snapshot, prepared.prompt
    native_tools = tools_from_specs(initial_snapshot.frozen_specs)
    pending_native_messages: list[ProviderToolResult] | None = None
    prompt, pending_native_messages = _apply_recovery_first(
        session,
        native,
        pending_initial,
        prompt,
        pending_native_messages,
        provider_session_changed=provider_session_changed,
        format_results=_prompt._format_results,
        native_tool_messages=_transport._native_tool_results,
        replayed_result_ids=replayed_result_ids,
    )
    return _KernelStartup(
        executors=runnable, delivered=delivered_map, max_turns=max_turns,
        native=native, identity_ref=identity_ref,
        state=_ProviderTurnState(prompt=prompt,
                                 pending_reply=initial_turn.reply if initial_turn is not None else None,
                                 pending_messages=pending_native_messages),
        native_tools=native_tools, snapshot=initial_snapshot, pending_recovery=bool(pending_initial),
        attempt_gate_bound=attempt_gate_bound,
        # Keep the exact reference carried by the approval/recovery row.  The
        # caller may not reconstruct it from the current provider binding when
        # a continuation crossed a run boundary.
        window_ref=recovered_window or active_window,
    )


def _completion_context_with_ignores(context: Any, ignored_paths: Any) -> Any:
    if context is None or isinstance(context, dict):
        return {**(context or {}), "workspace_ignored_paths": tuple(ignored_paths or ())}
    return context


def _resume_start(value: object) -> int:
    if value is None:
        return 1
    coordinates: tuple[int, int] = effect_coordinates(value, 0)
    turn, _ = coordinates
    return max(1, turn)


def _call_boundary(
    callback: Callable[[str], str | None] | None,
    boundary: str,
    turns_used: int,
    *,
    propagate: bool,
) -> KernelResult | str:
    if callback is None:
        return ""
    try:
        return str(callback(boundary) or "")
    except Exception as exc:
        return _provider_failure(exc, turns_used, propagate=propagate)


def _settle_at_boundary(
    result: KernelResult,
    boundary: str,
    on_boundary: Callable[[str], str | None] | None,
    turns_used: int,
    *,
    propagate: bool,
    only_stop_reasons: tuple[str, ...] = (),
) -> KernelResult:
    if only_stop_reasons and result.stop_reason not in only_stop_reasons:
        return result
    settled = _call_boundary(on_boundary, 'approval' if boundary == 'approval' else 'settle', turns_used, propagate=propagate)
    return settled if isinstance(settled, KernelResult) else result


def _finish_or_steer(action: Any, on_boundary: Callable[[str], str | None] | None,
                     turn: int, max_turns: int, *, propagate: bool) -> Any:
    """A late steer continues this task only when another generation can run."""
    if not isinstance(action, KernelResult) or action.stop_reason != 'done':
        return action
    boundary = 'final' if turn < max_turns else 'settle'
    steering = _call_boundary(on_boundary, boundary, turn, propagate=propagate)
    if isinstance(steering, KernelResult):
        return steering
    return (f'New user input at provider boundary:\n{steering}', None) if steering else action


def run_task_kernel(
    session: TaskSession,
    *,
    request: KernelRunRequest,
) -> KernelResult:
    transport, execution, observation = (
        request.transport, request.execution, request.observation,
    )
    provider = transport.provider
    provider_id = transport.provider_id
    project_path = execution.project_path
    stop_flag = transport.stop_flag
    stagnant_turns = transport.stagnant_turns
    intent_sink = observation.intent_sink
    on_event = observation.on_event
    propagate_provider_failure = observation.propagate_provider_failure
    on_boundary = observation.on_boundary
    completion_context = _completion_context_with_ignores(
        observation.completion_context, execution.workspace_ignored_paths,
    )
    try:
        attempt_gate_bound = _bind_attempt_gate(provider, transport, observation.trace_recorder)
        resume_start = _resume_start(transport.start_turn)
        startup = _kernel_startup(
            session,
            provider,
            executors=execution.executors,
            run_id=transport.run_id,
            effect_scope=(transport.effect_scope or ""),
            provider_id=provider_id,
            user_task=transport.user_task,
            context_text=transport.context_text,
            task_guidance=request.task_guidance,
            completion_context=completion_context,
            initial_results=transport.initial_results,
            provider_session_changed=transport.provider_session_changed,
            delivered=transport.delivered,
            initial_turn=transport.initial_turn,
            replayed_result_ids=transport.replayed_result_ids,
            attempt_gate_bound=attempt_gate_bound,
        )
    except RecoveryFailed as exc:
        # Recovery delivery failures must never fall through to the initial
        # prompt: the model would continue without the recovered results.
        return KernelResult(
            completed=False,
            summary=f"recovery failed: {exc}",
            turns=0,
            stop_reason="recovery_failure",
        )
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"controller configuration error: {exc}", turns=0, stop_reason="controller_failure"
        )
    turns_used = 0
    state = startup.state
    native = startup.native
    native_tools = startup.native_tools
    window_ref = startup.window_ref or transport.window_ref
    progress = KernelProgress(stagnant_turns)
    prev_contract = startup.snapshot.contract_text
    for turn in range(resume_start, startup.max_turns + 1):
        stopped = _stop_at_turn_start(
            stop_flag, provider, state.pending_reply, state.pending_messages,
            turns_used, propagate_provider_failure=propagate_provider_failure,
            declared_tools=native_tools,
        )
        if stopped is not None:
            return stopped
        session.turn = turn
        turns_used = turn
        # First round reuses the startup snapshot (same session facts); later
        # rounds rebuild once per round. Saves one snapshot build per run.
        # 同一 TurnSnapshot 贯穿发送、解析与执行，注册表变化下一轮生效。
        if turn == resume_start and not startup.pending_recovery:
            turn_snapshot = startup.snapshot
            controller = turn_snapshot.allowed
        else:
            snapshot_out = _rebuild_turn_snapshot(
                session, native=native,
                prompt=state.prompt, prev_contract=prev_contract,
            )
            if isinstance(snapshot_out, KernelResult):
                return snapshot_out
            controller, native_tools, state.prompt, prev_contract, turn_snapshot = snapshot_out
        received = _receive_turn_plan(
            session, provider, state, turn_snapshot, native_tools,
            native=native, provider_id=provider_id, turn=turn,
            stop_flag=stop_flag, on_event=on_event, stagnant_turns=stagnant_turns,
            propagate_provider_failure=propagate_provider_failure,
            trace_recorder=observation.trace_recorder,
            ghost_sources=transport.ghost_sources,
            require_ghost_sources=(turn == resume_start and not startup.attempt_gate_bound),
            context_scope=transport.context_scope,
            admit_hints=transport.admit_hints,
            kernel_admission=not startup.attempt_gate_bound,
            window_ref=window_ref,
        )
        if isinstance(received, KernelResult):
            # A received protocol failure has no following tool batch, so its
            # deferred provider effect must be settled before the verdict.
            return _settle_at_boundary(
                received, "final", on_boundary, turns_used,
                propagate=propagate_provider_failure, only_stop_reasons=("protocol",),
            )
        if received is None:
            continue
        plan, reply = received.plan, received.reply
        calls = list(plan.calls or ())
        done_action, approval, calls = _kernel_handle_done_approval(
            session,
            plan,
            calls,
            provider,
            reply,
            native,
            native_tools,
            completion_context=completion_context,
            project_path=project_path,
            on_shell_request=observation.on_shell_request,
            turn=turn,
            identity_ref=startup.identity_ref,
            intent_sink=intent_sink,
            permission_profile=execution.permission_profile,
            provider_id=provider_id,
            on_event=on_event,
            durable=execution.durable,
            managed_outputs=execution.managed_outputs,
            run_id=transport.run_id,
        )
        done_action = _finish_or_steer(done_action, on_boundary, turn, startup.max_turns,
                                      propagate=propagate_provider_failure)
        if isinstance(done_action, KernelResult):
            return done_action
        if done_action is not None:
            state.prompt, state.pending_reply = done_action
            continue
        if approval is not None:
            # A shell approval pauses between provider generation and tool
            # execution. Persist the deferred provider settlement and its
            # assistant tool-call history before waiting; the approval
            # result can then append the matching tool result atomically.
            return _settle_at_boundary(
                cast(KernelResult, approval), "approval", on_boundary, turns_used,
                propagate=propagate_provider_failure,
            )
        blocked = _ensure_workspace_writer(
            execution.ensure_workspace_writer, calls, provider, reply,
            native_tools, turns_used, propagate_provider_failure,
        )
        if blocked is not None:
            # No tool batch follows this generation, but its provider effect
            # was still received. Settle it at the final boundary so the
            # outer writer verdict does not run against a pending provider.
            return _settle_at_boundary(
                blocked, "final", on_boundary, turns_used,
                propagate=propagate_provider_failure,
            )
        _events._emit_tool_starts(on_event, session, turn, calls)
        _outer_evidence = _outer_evidence_for_context(completion_context)
        results_or_failure = _call_execute_turn(
            session, calls, startup.executors, transport.run_id, transport.effect_scope, turn,
            project_path, execution.tool_fns, execution.research_tools, execution.change_tracker,
            execution.managed_outputs, execution.session_id, execution.permission_profile,
            startup.delivered, intent_sink, controller, _outer_evidence,
            execution.workspace_ignored_paths, execution.workspace_revision_store,
            turns_used, propagate_provider_failure, stop_flag,
            durable=execution.durable,
            snapshot=turn_snapshot,
            on_event=on_event,
        )
        if isinstance(results_or_failure, KernelResult):
            # An execution-layer failure can end the run before post_tools.
            # The provider generation was already received, so settle it at
            # the final boundary before the outer lifecycle settles the
            # writer/repair state.
            return _settle_at_boundary(
                results_or_failure, "final", on_boundary, turns_used,
                propagate=propagate_provider_failure,
            )
        results = results_or_failure
        boundary_prompt = _call_boundary(
            on_boundary, "post_tools", turns_used, propagate=propagate_provider_failure,
        )
        if isinstance(boundary_prompt, KernelResult):
            return boundary_prompt
        try:
            _events._emit_tool_results(on_event, session, results, run_id=startup.identity_ref, turn=turn)
        except RecoveryFailed as exc:
            return KernelResult(
                completed=False,
                summary=f"recovery failed: {exc}",
                turns=turns_used,
                stop_reason="recovery_failure",
            )
        advance = _advance_after_results(
            native, results, session, state.pending_messages,
            completion_context=completion_context,
            boundary_prompt=boundary_prompt,
        )
        if isinstance(advance, KernelResult):
            return advance
        state.pending_messages, state.prompt = advance
        stopped = _stop_no_progress(progress, results, session, provider, state.pending_messages,
                                    turns_used, stop_flag=stop_flag, propagate=propagate_provider_failure,
                                    declared_tools=native_tools)
        if stopped is not None:
            return _settle_at_boundary(stopped, "final", on_boundary, turns_used,
                propagate=propagate_provider_failure, only_stop_reasons=("no_progress", "blocked"))
    finished = _finish_after_budget(
        session,
        provider,
        state.pending_messages,
        state.pending_reply,
        turns_used,
        propagate_provider_failure=propagate_provider_failure,
        declared_tools=native_tools,
    )
    if finished.stop_reason in {"max_turns", "protocol"}:
        # The turn budget can stop after a completed provider generation whose
        # settlement was deferred (for example a rejected done without tools).
        # Settle it at the final boundary before the outer lifecycle records
        # the writer/repair verdict; otherwise the pending provider effect
        # makes the settle transition illegal.
        boundary_result = _call_boundary(
            on_boundary, "settle", turns_used, propagate=propagate_provider_failure,
        )
        if isinstance(boundary_result, KernelResult):
            return boundary_result
    return finished


def _stop_at_turn_start(
    stop_flag: Any, provider: Any, pending_reply: Any, pending_messages: Any,
    turns_used: int, *, propagate_provider_failure: bool, declared_tools: Any = (),
) -> KernelResult | None:
    """Turn-start stop: deliver pending native results, then report stopped.

    Returns None to continue; otherwise the terminal result. Delivery
    failures report provider_failure, never a clean close.
    """
    if not _stop_requested(stop_flag):
        return None
    if pending_messages:
        try:
            _transport._drain_native_budget(provider, pending_messages, declared_tools=declared_tools)
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
        return KernelResult(completed=False, summary="stopped", turns=turns_used, stop_reason="stopped")
    if pending_reply is not None and not isinstance(pending_reply, str):
        try:
            _transport.close_native_reply(
                provider, pending_reply, "task stopped; call not executed",
                declared_tools=declared_tools,
            )
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
    return KernelResult(completed=False, summary="stopped", turns=turns_used, stop_reason="stopped")


def _ensure_workspace_writer(
    ensure: Callable[[], None] | None, calls: list[ToolCall], provider: Any, reply: Any,
    native_tools: Any, turns_used: int, propagate: bool,
) -> KernelResult | None:
    if ensure is None or not any(call.name in {"edit", "run"} for call in calls):
        return None
    try:
        ensure()
    except Exception as exc:
        from codey.workspace.changes import ProjectWriteBusy

        try:
            _transport.close_native_reply(provider, reply, f"workspace write blocked: {exc}",
                                          declared_tools=native_tools)
        except Exception as delivery_error:
            return _provider_failure(delivery_error, turns_used, propagate=propagate)
        reason = "project_write_busy" if isinstance(exc, ProjectWriteBusy) else "controller_failure"
        return KernelResult(False, f"workspace write blocked: {exc}", turns_used, reason)
    return None


def _cancel_after_send(stop_flag: Any, provider: Any, reply: Any, native: Any, turns_used: Any, *,
                       propagate: Any, declared_tools: Any = ()) -> Any:
    if not _stop_requested(stop_flag):
        return None
    # A cancellation arriving during a slow send wins over returned tool calls.
    if native:
        try:
            _transport.close_native_reply(provider, reply, "task stopped; call not executed", declared_tools=declared_tools)
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate)
    return KernelResult(False, "stopped", turns_used, "stopped")


def _stop_no_progress(progress: Any, results: Any, session: Any, provider: Any, messages: Any, turns_used: Any, *, propagate: Any,
                      stop_flag: Any = None, declared_tools: Any = ()) -> Any:
    cancelled = _stop_requested(stop_flag)
    if not cancelled and not progress.observe(results, session):
        return None
    if messages:
        try:
            _transport._drain_native_budget(provider, messages, declared_tools=declared_tools)
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate)
    if cancelled:
        return KernelResult(False, "stopped", turns_used, "stopped")
    from codey.operations.explicit_execution_requirements import readonly_fix_obstruction

    obstruction = readonly_fix_obstruction(session)
    if obstruction is not None:
        return KernelResult(False, obstruction, turns_used, "blocked")
    return KernelResult(False, "stopped after repeated tool results without progress", turns_used, "no_progress")


def _rebuild_turn_snapshot(
    session: Any, *, native: bool,
    prompt: str, prev_contract: str,
) -> Any:
    """Rebuild one per-round snapshot; KernelResult on controller failure."""
    try:
        snapshot = kernel_protocol.build_turn_snapshot(session, native=native)
        turn_native_tools = tools_from_specs(snapshot.frozen_specs)
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"controller configuration error: {exc}",
            turns=int(getattr(session, "turn", 0) or 0), stop_reason="controller_failure",
        )
    # One snapshot per round: compare this round's contract with the previous
    # round's. Web text chains have no per-turn schema, so a changed contract
    # is prepended to the carried prompt here; native chains get the fresh
    # schema via send_turn/send_tool_results.
    fresh_text = snapshot.contract_text
    if (not native) and fresh_text and fresh_text != prev_contract and prompt:
        names = ", ".join(snapshot.tool_names) or "none"
        prompt = (
            f"Visible tools changed (controller state advanced): {names}\n"
            f"Tool contract (use exactly these shapes):\n{fresh_text}\n\n{prompt}"
        )
    if fresh_text:
        prev_contract = fresh_text
    return snapshot.allowed, turn_native_tools, prompt, prev_contract, snapshot


def _call_execute_turn(
    session: Any, calls: Any, runnable: Any, run_id: Any, effect_scope: Any, turn: Any,
    project_path: Any, tool_fns: Any, research_tools: Any, change_tracker: Any,
    managed_outputs: Any, session_id: Any, permission_profile: Any,
    delivered_map: Any, intent_sink: Any, controller: Any, outer_evidence: Any,
    workspace_ignored_paths: Any, workspace_revision_store: Any,
    turns_used: int, propagate_provider_failure: bool, stop_flag: Any = None,
    durable: Any = None,
    snapshot: Any = None,
    on_event: Any = None,
) -> Any:
    """Run one execution turn; faults fail closed as provider_failure."""
    try:
        effective_runnable = dict(runnable or {})
        if "codemode" not in effective_runnable:
            from codey.codemode.kernel import build_execute_turn_executor

            effective_runnable["codemode"] = build_execute_turn_executor(
                session=session,
                snapshot=snapshot,
                run_id=run_id,
                effect_scope=effect_scope,
                turn=int(turn),
                project_path=project_path,
                tool_fns=tool_fns,
                research_tools=research_tools,
                change_tracker=change_tracker,
                managed_outputs=managed_outputs,
                session_id=session_id,
                permission_profile=permission_profile,
                delivered=delivered_map or None,
                intent_sink=intent_sink,
                controller_allowed=controller,
                execution_evidence=outer_evidence,
                workspace_ignored_paths=workspace_ignored_paths,
                workspace_revision_store=workspace_revision_store,
                stop_flag=stop_flag,
                executors=effective_runnable,
                durable=durable,
                allow_writes=permission_profile == "coding_writer" and project_path is not None,
                on_event=on_event,
            )
        return _execute_turn(
            session, calls, executors=effective_runnable, run_id=run_id,
            effect_scope=effect_scope, turn=turn, project_path=project_path,
            tool_fns=tool_fns, research_tools=research_tools,
            change_tracker=change_tracker, managed_outputs=managed_outputs,
            session_id=session_id, permission_profile=permission_profile,
            delivered=delivered_map or None, intent_sink=intent_sink,
            controller_allowed=controller, execution_evidence=outer_evidence,
            workspace_ignored_paths=workspace_ignored_paths,
            workspace_revision_store=workspace_revision_store,
            stop_flag=stop_flag, snapshot=snapshot, durable=durable,
        )
    except Exception as exc:
        # Fail closed: an execution-layer fault (e.g. durable settle) never
        # bubbles as an unhandled exception; it terminates as provider
        # failure so receipts/ledger stay consistent.
        return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)


def _advance_after_results(
    native: bool,
    results: list[ToolResult],
    session: TaskSession,
    pending_native_messages: list[ProviderToolResult] | None,
    *,
    completion_context: Any = None,
    boundary_prompt: str = "",
) -> tuple[list[ProviderToolResult] | None, str] | KernelResult:
    """Deliver native receipts or build the next text prompt."""
    if native:
        try:
            messages = _transport._native_tool_results(
                results, session, boundary_prompt=boundary_prompt,
            )
        except ValueError as exc:
            return KernelResult(
                completed=False,
                summary=f"native mixed call ids: {exc}",
                turns=int(getattr(session, "turn", 0) or 0),
                stop_reason="protocol",
            )
        if messages:
            return messages, ""
    base_prompt = _prompt._format_results(results, session)
    if boundary_prompt:
        base_prompt = f"{base_prompt}\n\nNew user input at provider boundary:\n{boundary_prompt}"
    prepared = prepare_coding_context(session, completion_context=completion_context)
    current_context = render_coding_context(prepared) if prepared is not None else ""
    if current_context:
        base_prompt = f"{base_prompt}\n\n{current_context}"
    return pending_native_messages, base_prompt


def _finish_after_budget(
    session: TaskSession,
    provider: Any,
    pending_native_messages: list[ProviderToolResult] | None,
    pending_reply: Any,
    turns_used: int,
    *,
    propagate_provider_failure: bool,
    declared_tools: Any = (),
) -> KernelResult:
    if pending_native_messages or pending_reply is not None:
        # Native APIs require a reply for every emitted tool-call id. Deliver
        # the final batch even when the turn budget stops further work.
        try:
            if pending_native_messages:
                _transport._drain_native_budget(provider, pending_native_messages, declared_tools=declared_tools)
            if pending_reply is not None:
                _transport.close_native_reply(
                    provider, pending_reply, "turn budget exhausted; tool call was not executed",
                    declared_tools=declared_tools,
                )
        except _transport.NativeBudgetExhausted:
            return KernelResult(
                False, "native tool chain exceeded budget drain limit", turns_used, "protocol")
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
    partial = str(getattr(session, "last_done_text", "") or "").strip()
    return KernelResult(
        completed=False, summary=partial or "max turns reached", turns=turns_used, stop_reason="max_turns"
    )


def _handle_done_reply(
    session: TaskSession,
    plan: ToolPlan,
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    *,
    completion_context: Any = None,
) -> tuple[str, Any] | KernelResult | None:
    """Evaluate one done proposal; fail closed with the call id answered."""

    session.last_done_text = (plan.control.body or "") if plan.control is not None else ""
    session.last_done_args = dict(plan.control_args)
    try:
        from codey.operations.completion_gate import evaluate as gate_evaluate
        from codey.operations.project_verification import refresh_verification_candidates

        refresh_verification_candidates(session)
        verdict = gate_evaluate(session, session.last_done_text, context=completion_context)
        if type(getattr(verdict, "complete", None)) is not bool:
            raise ValueError("completion gate returned an invalid verdict")
    except Exception as exc:
        prompt = _prompt._completion_followup(
            f"Completion check failed ({exc}); cannot complete yet. Continue the task.", native=native)
        try:
            pending = _transport._take_answered_reply(provider, reply, native, native_tools, prompt)
        except Exception as receipt_exc:
            return KernelResult(
                completed=False, summary=f"provider failed: {receipt_exc}",
                turns=(session.turn or 0), stop_reason="provider_failure",
            )
        return prompt, pending
    if verdict.complete is True:
        session.last_done_text = str(getattr(verdict, "final_text", "") or session.last_done_text)
        # Native chains require every call id closed, including an accepted
        # done. Answer the done id with success before returning so the same
        # session can continue and the provider chain stays legal. A failed
        # receipt never counts as closed: surface provider failure / pending
        # delivery instead of claiming completion.
        has_calls = native and not isinstance(reply, str) and bool(getattr(reply, "tool_calls", ()))
        if has_calls:
            try:
                followup = provider.acknowledge_tool_results(
                    [ProviderToolResult(call.id, f"OK: done accepted: {session.last_done_text[:500]}")
                     for call in reply.tool_calls], native_tools,
                )
                if followup is not None and not isinstance(followup, str):
                    _transport.close_native_reply(
                        provider,
                        followup,
                        "task already completed; tool call was not executed",
                        declared_tools=native_tools,
                    )
            except Exception as exc:
                return KernelResult(
                    completed=False,
                    summary=f"provider failed delivering done receipt: {exc}",
                    turns=(session.turn or 0),
                    stop_reason="delivery_pending",
                    proof=getattr(verdict, "proof", None),
                    delivery="unknown" if getattr(exc, "provider_failure_kind", "") == "submission_uncertain" else "failed",
                )
        return KernelResult(
            completed=True, summary=session.last_done_text, turns=(session.turn or 0), stop_reason="done",
            proof=getattr(verdict, "proof", None),
            delivery="success" if has_calls else "not_required",
        )
    prompt = _prompt._completion_followup(verdict.followup, native=native)
    try:
        pending = _transport._take_answered_reply(provider, reply, native, native_tools, prompt)
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"provider failed: {exc}",
            turns=(session.turn or 0), stop_reason="provider_failure",
        )
    return prompt, pending
