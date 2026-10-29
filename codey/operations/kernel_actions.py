"""Execution wiring reused by normal operation actions and restart actions.

These helpers own resource checks, tool dispatch and prompt construction;
only the operation reducer chooses the next action.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from codey.operations import kernel_prompt as _prompt
from codey.operations import kernel_transport as _transport
from codey.operations.kernel_contracts import KernelResult, KernelTransportDeps
from codey.operations.kernel_execution import execute_turn as _execute_turn
from codey.operations.kernel_trace import record_outbound
from codey.operations.project_prompt_context import prepare_coding_context
from codey.operations.task_session import turn_effect_id as _turn_effect_id
from codey.policies.command_line import ShellSpec
from codey.providers.base import ProviderToolResult
from codey.runtime.core.models import ToolCall, ToolResult
from codey.workspace.coding_context import render_coding_context
from codey.workspace.context_admission import admit_ghost_context
from codey.workspace.context_epoch import ContextEpoch, context_epoch_id

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession

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


def _outer_evidence_for_context(completion_context: Any) -> Any:
    try:
        if isinstance(completion_context, dict):
            return completion_context.get("execution_evidence")
        return getattr(completion_context, "execution_evidence", None)
    except Exception:
        return None


def _completion_context_with_ignores(context: Any, ignored_paths: Any) -> Any:
    if context is None or isinstance(context, dict):
        return {**(context or {}), "workspace_ignored_paths": tuple(ignored_paths or ())}
    return context


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
    text_result_spans: list[dict[str, Any]] | None = None,
) -> tuple[list[ProviderToolResult] | None, str] | KernelResult:
    """Deliver native receipts or build the next text prompt."""
    if native:
        from codey.operations.kernel_errors import RecoveryFailed
        try:
            messages = _transport._native_tool_results(results, session, boundary_prompt=boundary_prompt)
        except ValueError as exc:
            raise RecoveryFailed(f"native result delivery is invalid: {exc}") from exc
        if not messages:
            raise RecoveryFailed("native result delivery has no original call ids")
        return messages, ""
    base_prompt = _prompt._format_results(results, session, text_result_spans=text_result_spans)
    if boundary_prompt:
        base_prompt = f"{base_prompt}\n\nNew user input at provider boundary:\n{boundary_prompt}"
    prepared = prepare_coding_context(session, completion_context=completion_context)
    current_context = render_coding_context(prepared) if prepared is not None else ""
    if current_context:
        base_prompt = f"{base_prompt}\n\n{current_context}"
    return pending_native_messages, base_prompt
