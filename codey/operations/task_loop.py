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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from codey.operations.kernel_trace import record_turn
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import effect_coordinates
from codey.operations.task_session import turn_effect_id as _turn_effect_id
from codey.providers.base import ProviderToolDefinition, ProviderToolResult, TurnFinish, tools_from_specs
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult
from codey.workspace.coding_context import render_coding_context

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
    stop_flag: Any = None
    stagnant_turns: int | None = None
    delivered: Mapping[str, ToolResult] | None = None
    start_turn: int | None = None
    initial_results: list[ToolResult] | None = None
    provider_session_changed: bool = False
    initial_turn: InitialNativeTurn | None = None


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


@dataclass(frozen=True)
class KernelObservationDeps:
    """Completion, recovery, and event sinks for one kernel run."""

    intent_sink: Any = None
    completion_context: Any = None
    on_event: Callable[[Any], None] | None = None
    on_shell_request: Callable[[Any], None] | None = None
    trace_recorder: Any = None
    propagate_provider_failure: bool = False


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


def _receive_turn_plan(
    session: TaskSession, provider: Any, state: _ProviderTurnState, snapshot: Any, native_tools: Any,
    *, native: bool, provider_id: object, turn: int, stop_flag: Any, on_event: Any,
    stagnant_turns: int | None, propagate_provider_failure: bool, trace_recorder: Any,
) -> _ReceivedPlan | KernelResult | None:
    """Receive once; cancellation, truncation and protocol repair precede execution.

    None requests the next turn using the updated transport state. This helper
    never loops, executes tools, or accepts completion.
    """
    try:
        if state.pending_reply is None:
            from codey.operations.kernel_prompt import working_context

            update = getattr(provider, "set_working_context", None)
            if callable(update):
                update(working_context(session))
            refs = getattr(provider, "set_result_refs", None)
            if callable(refs):
                refs(tuple(session._memory_results))
        reply, state.pending_reply, state.pending_messages = _transport.send_kernel_reply(
            provider, native, state.prompt, native_tools, state.pending_reply, state.pending_messages,
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
    plan = _normalize_turn(reply, snapshot=snapshot)
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
    if propagate:
        raise exc
    from codey.runtime.core.cancellation import TaskCancelled

    if isinstance(exc, TaskCancelled):
        return KernelResult(completed=False, summary="stopped", turns=turns_used, stop_reason="stopped")
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
) -> KernelResult | None:
    if on_shell_request is None or project_path is None:
        return None
    from codey.agents.shell_approval import ShellApprovalRequest, deferred_tool_call_from_call
    from codey.agents.tool_execution import evaluate_tool_call_policy_for, policy_asks_user
    from codey.operations.task_execution import effective_project_profile

    try:
        allows_shell = bool(policy.allows("shell.approval")) if policy is not None else False
    except Exception:
        allows_shell = False
    if not allows_shell:
        # No TaskPolicy grant: read-only and unauthorized Research never
        # pause here. Execution still denies via the project guard.
        return None
    effective_profile = effective_project_profile(permission_profile)
    for index, call in enumerate(calls):
        if call.name != "shell":
            continue
        decision, _replay = evaluate_tool_call_policy_for(
            call,
            project=project_path,
            permission_profile=effective_profile,
            approval_available=True,
            phase="writer",
        )
        if not policy_asks_user(decision):
            return None
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
                turn=int(turn),
                tool_index=int(index),
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
    if stagnant_turns is not None and invalid_turns >= max(1, int(stagnant_turns)):
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
        pending_reply = _transport.repair_native_dangling(provider, reply, native, native_tools, plan.protocol_error)
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
) -> tuple[Any, Any, list[Any]]:
    """Handle done/approval; returns (done_action, approval, calls).

    done_action is None (proceed), KernelResult (return), or tuple (prompt, pending) to continue.
    approval is None (proceed) or KernelResult (return).
    """
    done_action = None
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
) -> _KernelStartup:
    runnable = dict(executors or {})
    delivered_map = dict(delivered or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    pending_initial = list(initial_results or [])
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
        provider_session_changed=bool(provider_session_changed),
        format_results=_prompt._format_results,
        native_tool_messages=_transport._native_tool_results,
    )
    return _KernelStartup(
        executors=runnable, delivered=delivered_map, max_turns=max_turns,
        native=native, identity_ref=identity_ref,
        state=_ProviderTurnState(prompt=prompt,
                                 pending_reply=initial_turn.reply if initial_turn is not None else None,
                                 pending_messages=pending_native_messages),
        native_tools=native_tools, snapshot=initial_snapshot, pending_recovery=bool(pending_initial),
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


def run_task_kernel(
    session: TaskSession,
    *,
    request: KernelRunRequest,
) -> KernelResult:
    provider, executors = request.transport.provider, request.execution.executors
    run_id, effect_scope, provider_id = (
        request.transport.run_id, request.transport.effect_scope, request.transport.provider_id
    )
    project_path = request.execution.project_path
    tool_fns = request.execution.tool_fns
    research_tools = request.execution.research_tools
    change_tracker = request.execution.change_tracker
    managed_outputs = request.execution.managed_outputs
    session_id = request.execution.session_id
    permission_profile = request.execution.permission_profile
    user_task = request.transport.user_task
    context_text = request.transport.context_text
    stop_flag = request.transport.stop_flag
    stagnant_turns = request.transport.stagnant_turns
    delivered = request.transport.delivered
    intent_sink = request.observation.intent_sink
    completion_context = request.observation.completion_context
    on_event = request.observation.on_event
    on_shell_request = request.observation.on_shell_request
    propagate_provider_failure = request.observation.propagate_provider_failure
    start_turn = request.transport.start_turn
    initial_results = request.transport.initial_results
    provider_session_changed = request.transport.provider_session_changed
    workspace_ignored_paths = request.execution.workspace_ignored_paths
    workspace_revision_store = request.execution.workspace_revision_store
    trace_recorder = request.observation.trace_recorder
    completion_context = _completion_context_with_ignores(completion_context, workspace_ignored_paths)
    try:
        resume_start = _resume_start(start_turn)
        startup = _kernel_startup(
            session,
            provider,
            executors=executors,
            run_id=run_id,
            effect_scope=str(effect_scope or ""),
            provider_id=provider_id,
            user_task=user_task,
            context_text=context_text,
            task_guidance=request.task_guidance,
            completion_context=completion_context,
            initial_results=initial_results,
            provider_session_changed=bool(provider_session_changed),
            delivered=delivered,
            initial_turn=request.transport.initial_turn,
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
            propagate_provider_failure=propagate_provider_failure, trace_recorder=trace_recorder,
        )
        if isinstance(received, KernelResult):
            return received
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
            on_shell_request=on_shell_request,
            turn=turn,
            identity_ref=startup.identity_ref,
            intent_sink=intent_sink,
            permission_profile=permission_profile,
            provider_id=provider_id,
        )
        if isinstance(done_action, KernelResult):
            return done_action
        if done_action is not None:
            state.prompt, state.pending_reply = done_action
            continue
        if approval is not None:
            return cast(KernelResult, approval)
        _events._emit_tool_starts(on_event, session, turn, calls)
        _outer_evidence = _outer_evidence_for_context(completion_context)
        results_or_failure = _call_execute_turn(
            session, calls, startup.executors, run_id, effect_scope, turn,
            project_path, tool_fns, research_tools, change_tracker,
            managed_outputs, session_id, permission_profile,
            startup.delivered, intent_sink, controller, _outer_evidence,
            workspace_ignored_paths, workspace_revision_store,
            turns_used, propagate_provider_failure, stop_flag,
            snapshot=turn_snapshot,
        )
        if isinstance(results_or_failure, KernelResult):
            return results_or_failure
        results = results_or_failure
        try:
            _events._emit_tool_results(on_event, session, results, run_id=startup.identity_ref, turn=turn)
        except RecoveryFailed as exc:
            return KernelResult(
                completed=False,
                summary=f"recovery failed: {exc}",
                turns=turns_used,
                stop_reason="recovery_failure",
            )
        advance = _advance_after_results(native, results, session, state.pending_messages,
                                         completion_context=completion_context)
        if isinstance(advance, KernelResult):
            return advance
        state.pending_messages, state.prompt = advance
        stopped = _stop_no_progress(progress, results, session, provider, state.pending_messages,
                                    turns_used, stop_flag=stop_flag, propagate=propagate_provider_failure,
                                    declared_tools=native_tools)
        if stopped is not None:
            return cast(KernelResult, stopped)
    return _finish_after_budget(
        session,
        provider,
        state.pending_messages,
        state.pending_reply,
        turns_used,
        propagate_provider_failure=propagate_provider_failure,
        declared_tools=native_tools,
    )


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
    snapshot: Any = None,
) -> Any:
    """Run one execution turn; faults fail closed as provider_failure."""
    try:
        return _execute_turn(
            session, calls, executors=runnable, run_id=run_id,
            effect_scope=effect_scope, turn=turn, project_path=project_path,
            tool_fns=tool_fns, research_tools=research_tools,
            change_tracker=change_tracker, managed_outputs=managed_outputs,
            session_id=session_id, permission_profile=permission_profile,
            delivered=delivered_map or None, intent_sink=intent_sink,
            controller_allowed=controller, execution_evidence=outer_evidence,
            workspace_ignored_paths=workspace_ignored_paths,
            workspace_revision_store=workspace_revision_store,
            stop_flag=stop_flag, snapshot=snapshot,
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
) -> tuple[list[ProviderToolResult] | None, str] | KernelResult:
    """Deliver native receipts or build the next text prompt."""
    if native:
        try:
            messages = _transport._native_tool_results(results, session)
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

    session.last_done_text = str(plan.control.body or "") if plan.control is not None else ""
    session.last_done_args = dict(plan.control_args)
    try:
        from codey.operations.completion_gate import evaluate as gate_evaluate
        from codey.operations.project_verification import refresh_verification_candidates

        refresh_verification_candidates(session)
        verdict = gate_evaluate(session, session.last_done_text, context=completion_context)
    except Exception as exc:
        prompt = f"Completion check failed ({exc}); cannot complete yet. Continue the task."
        try:
            pending = _transport._take_answered_reply(provider, reply, native, native_tools, prompt)
        except Exception as receipt_exc:
            return KernelResult(
                completed=False, summary=f"provider failed: {receipt_exc}",
                turns=int(session.turn or 0), stop_reason="provider_failure",
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
                    turns=int(session.turn or 0),
                    stop_reason="delivery_pending",
                    proof=getattr(verdict, "proof", None),
                    delivery="unknown" if getattr(exc, "provider_failure_kind", "") == "submission_uncertain" else "failed",
                )
        return KernelResult(
            completed=True, summary=session.last_done_text, turns=int(session.turn or 0), stop_reason="done",
            proof=getattr(verdict, "proof", None),
            delivery="success" if has_calls else "not_required",
        )
    try:
        pending = _transport._take_answered_reply(provider, reply, native, native_tools, verdict.followup)
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"provider failed: {exc}",
            turns=int(session.turn or 0), stop_reason="provider_failure",
        )
    return verdict.followup, pending
