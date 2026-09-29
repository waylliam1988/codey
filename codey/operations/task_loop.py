"""One task tool loop for every task kind (operations layer).

Production topology: policy -> session -> snapshot -> adapter.send ->
normalize_turn -> done gate or execute_turn -> record -> deliver. Web text
JSON and native tool calls converge in ``kernel_protocol.normalize_turn``;
facts live in ``kernel_session.TaskSession``; project, web, and knowledge
tools dispatch through ``execute_turn``; ``done`` converges in the single
``completion_gate``. The loop sends exactly one provider message per
iteration (no double-send): a ``done`` rejection becomes the next prompt,
tool results become the next prompt (web) or the next tool_results call
(native) returning the following reply.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from codey.operations import kernel_events as _events
from codey.operations import kernel_prompt as _prompt
from codey.operations import kernel_transport as _transport
from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.kernel_execution import execute_turn as _execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot as _build_turn_snapshot
from codey.operations.kernel_protocol import normalize_turn as _normalize_turn
from codey.operations.kernel_recovery import apply_recovery_first as _apply_recovery_first
from codey.operations.task_session import turn_effect_id as _turn_effect_id
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult

if TYPE_CHECKING:  # Annotations only; the loop never re-exports TaskSession.
    from codey.operations.task_session import TaskSession

__all__ = [
    "KernelResult",
    "run_task_kernel",
]


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str


def _is_native_provider(provider: Any, *, provider_id: object = "") -> bool:
    return _transport.provider_uses_native(provider, provider_id=provider_id)


def _provider_failure(exc: Exception, turns_used: int, *, propagate: bool) -> KernelResult:
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


def _snapshot_for_turn_state(
    session: TaskSession,
    *,
    native: bool,
    user_task: object,
    context_text: str,
) -> tuple[Any, str, list[dict[str, Any]], str]:
    """One snapshot per turn: policy ∩ current facts for prompt/schemas/parse.

    Prompt, native schemas, parsing, and execution share this single built
    object. Any build failure raises and the kernel terminates the turn as
    controller_failure (fail-closed), never a stale fallback list.
    """
    snapshot = _build_turn_snapshot(session, native=native)
    prompt_now = _prompt.kernel_prompt_for_session(
        session,
        user_task=str(user_task or ""),
        contract_text=snapshot.contract_text,
        context_text=context_text,
        controller_allowed=snapshot.allowed,
        native=native,
    )
    return snapshot.allowed, snapshot.contract_text, list(snapshot.native_tools), prompt_now


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
) -> Any:
    """Handle one protocol error; None means proceed to tools."""
    if not getattr(plan, "protocol_error", ""):
        return None
    invalid_turns += 1
    if stagnant_turns is not None and invalid_turns >= max(1, int(stagnant_turns)):
        return KernelResult(
            False, f"stopped after {invalid_turns} invalid tool requests: {plan.protocol_error}", turn, "protocol"
        )
    prompt = _prompt._repair_prompt(plan.protocol_error)
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
    run_id: object,
    effect_scope: str,
    provider_id: object,
    user_task: object,
    context_text: str,
    start_turn: int | None,
    initial_results: list[ToolResult] | None,
    provider_session_changed: bool,
    delivered: Mapping[str, ToolResult] | None,
) -> tuple[dict[str, Any], dict[str, ToolResult], int, bool, str, str, list[dict[str, Any]], str, Any, Any, Any]:
    runnable = dict(executors or {})
    delivered_map = dict(delivered or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    pending_initial = list(initial_results or [])
    native = _is_native_provider(provider, provider_id=provider_id)
    identity_ref = f"{run_id}:{effect_scope}" if effect_scope else run_id
    initial_allowed, initial_contract, initial_native, initial_prompt = _snapshot_for_turn_state(
        session,
        native=native,
        user_task=user_task,
        context_text=context_text,
    )
    prompt: str = initial_prompt
    native_tools: list[dict[str, Any]] = initial_native
    pending_reply: Any = None
    pending_native_messages: list[dict[str, Any]] | None = None
    prompt, pending_native_messages = _apply_recovery_first(
        session,
        native,
        pending_initial,
        prompt,
        pending_native_messages,
        provider_session_changed=bool(provider_session_changed),
        format_results=_prompt._format_results,
        native_tool_messages=_transport._native_tool_messages,
    )
    return (
        runnable,
        delivered_map,
        max_turns,
        native,
        identity_ref,
        str(initial_contract or ""),
        native_tools,
        prompt,
        pending_reply,
        pending_native_messages,
        initial_allowed,
    )


def run_task_kernel(
    session: TaskSession,
    *,
    provider: Any,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    run_id: object = "",
    effect_scope: str = "",
    provider_id: object = "",
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    change_tracker: Any = None,
    managed_outputs: Any = None,
    session_id: str = "",
    permission_profile: str = "coding_writer",
    user_task: object = "",
    context_text: str = "",
    stop_flag: Any = None,
    stagnant_turns: int | None = None,
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: Any = None,
    completion_context: Any = None,
    on_event: Callable[[Any], None] | None = None,
    on_shell_request: Callable[[Any], None] | None = None,
    propagate_provider_failure: bool = False,
    start_turn: int | None = None,
    initial_results: list[ToolResult] | None = None,
    provider_session_changed: bool = False,
    workspace_ignored_paths: Any = (),
    workspace_revision_store: Any = None,
) -> KernelResult:
    try:
        (
            runnable,
            delivered_map,
            max_turns,
            native,
            identity_ref,
            initial_contract,
            native_tools,
            prompt,
            pending_reply,
            pending_native_messages,
            initial_allowed,
        ) = _kernel_startup(
            session,
            provider,
            executors=executors,
            run_id=run_id,
            effect_scope=str(effect_scope or ""),
            provider_id=provider_id,
            user_task=user_task,
            context_text=context_text,
            start_turn=start_turn,
            initial_results=initial_results,
            provider_session_changed=bool(provider_session_changed),
            delivered=delivered,
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
    try:
        resume_start = max(1, int(start_turn)) if start_turn is not None else 1
    except (TypeError, ValueError):
        resume_start = 1
    try:
        pending_initial = list(initial_results or [])
    except Exception:
        pending_initial = []
    turns_used = 0
    invalid_turns = 0
    prev_contract: str = str(initial_contract or "")
    for turn in range(resume_start, max_turns + 1):
        if _stop_requested(stop_flag):
            return KernelResult(completed=False, summary="stopped", turns=turns_used, stop_reason="stopped")
        session.turn = turn
        turns_used = turn
        # First round reuses the startup snapshot (same session facts); later
        # rounds rebuild once per round. Saves one snapshot build per run.
        if turn == resume_start and pending_reply is None and pending_native_messages is None and not pending_initial:
            controller, turn_contract, turn_native_tools, turn_prompt = (
                initial_allowed,
                initial_contract,
                native_tools,
                prompt,
            )
            prev_contract = str(turn_contract or "")
        else:
            snapshot_out = _rebuild_turn_snapshot(
                session, native=native, user_task=user_task, context_text=context_text,
                prompt=prompt, prev_contract=prev_contract,
            )
            if isinstance(snapshot_out, KernelResult):
                return snapshot_out
            controller, turn_contract, turn_native_tools, turn_prompt, prompt, prev_contract = snapshot_out
            native_tools = turn_native_tools
        try:
            reply, pending_reply, pending_native_messages = _transport.send_kernel_reply(
                provider,
                native,
                prompt,
                native_tools,
                pending_reply,
                pending_native_messages,
            )
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
        _events._emit_turn_event(on_event, turn, reply)
        plan = _normalize_turn(reply, policy=session.policy, controller_allowed=controller)
        protocol_action = _kernel_handle_protocol(
            plan,
            provider,
            reply,
            native,
            native_tools,
            stagnant_turns=stagnant_turns,
            invalid_turns=invalid_turns,
            turn=turn,
        )
        # protocol_action is None (continue to tools), KernelResult (return),
        # or tuple (prompt, pending_reply, invalid_turns) to continue.
        if isinstance(protocol_action, KernelResult):
            return protocol_action
        if protocol_action is not None:
            prompt, pending_reply, invalid_turns = protocol_action
            continue
        invalid_turns = 0
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
            identity_ref=identity_ref,
            intent_sink=intent_sink,
            permission_profile=permission_profile,
        )
        if isinstance(done_action, KernelResult):
            return done_action
        if done_action is not None:
            prompt, pending_reply = done_action
            continue
        if approval is not None:
            return approval
        _events._emit_tool_starts(on_event, session, turn, calls)
        _outer_evidence = _outer_evidence_for_context(completion_context)
        results_or_failure = _call_execute_turn(
            session, calls, runnable, run_id, effect_scope, turn,
            project_path, tool_fns, research_tools, change_tracker,
            managed_outputs, session_id, permission_profile,
            delivered_map, intent_sink, controller, _outer_evidence,
            workspace_ignored_paths, workspace_revision_store,
            turns_used, propagate_provider_failure,
        )
        if isinstance(results_or_failure, KernelResult):
            return results_or_failure
        results = results_or_failure
        try:
            _events._emit_tool_results(on_event, session, results, run_id=identity_ref, turn=turn)
        except RecoveryFailed as exc:
            return KernelResult(
                completed=False,
                summary=f"recovery failed: {exc}",
                turns=turns_used,
                stop_reason="recovery_failure",
            )
        advance = _advance_after_results(native, results, session, pending_native_messages)
        if isinstance(advance, KernelResult):
            return advance
        pending_native_messages, prompt = advance
        if pending_native_messages is not None and prompt == "":
            continue
    return _finish_after_budget(
        session,
        provider,
        pending_native_messages,
        native_tools,
        turns_used,
        propagate_provider_failure=propagate_provider_failure,
    )


def _rebuild_turn_snapshot(
    session: Any, *, native: bool, user_task: Any, context_text: Any,
    prompt: str, prev_contract: str,
) -> Any:
    """Rebuild one per-round snapshot; KernelResult on controller failure."""
    try:
        controller, turn_contract, turn_native_tools, turn_prompt = _snapshot_for_turn_state(
            session, native=native, user_task=user_task, context_text=context_text,
        )
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"controller configuration error: {exc}",
            turns=int(getattr(session, "turn", 0) or 0), stop_reason="controller_failure",
        )
    # One snapshot per round: compare this round's contract with the previous
    # round's. Web text chains have no per-turn schema, so a changed contract
    # is prepended to the carried prompt here; native chains get the fresh
    # schema via send_turn/send_tool_results.
    fresh_text = str(turn_contract or "")
    if (not native) and fresh_text and fresh_text != prev_contract and prompt:
        try:
            names = ", ".join(_prompt._snapshot_names(session.policy, controller)) or "none"
        except Exception:
            names = "none"
        prompt = (
            f"Visible tools changed (controller state advanced): {names}\n"
            f"Tool contract (use exactly these shapes):\n{fresh_text}\n\n{prompt}"
        )
    if fresh_text:
        prev_contract = fresh_text
    return controller, turn_contract, turn_native_tools, turn_prompt, prompt, prev_contract


def _call_execute_turn(
    session: Any, calls: Any, runnable: Any, run_id: Any, effect_scope: Any, turn: Any,
    project_path: Any, tool_fns: Any, research_tools: Any, change_tracker: Any,
    managed_outputs: Any, session_id: Any, permission_profile: Any,
    delivered_map: Any, intent_sink: Any, controller: Any, outer_evidence: Any,
    workspace_ignored_paths: Any, workspace_revision_store: Any,
    turns_used: int, propagate_provider_failure: bool,
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
    pending_native_messages: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]] | None, str] | KernelResult:
    """Deliver native receipts or build the next text prompt."""
    if native:
        try:
            messages = _transport._native_tool_messages(results, session)
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
    current_context = _prompt._coding_context_for_session(session)
    if current_context:
        base_prompt = f"{base_prompt}\n\n{current_context}"
    return pending_native_messages, base_prompt


def _finish_after_budget(
    session: TaskSession,
    provider: Any,
    pending_native_messages: list[dict[str, Any]] | None,
    native_tools: Any,
    turns_used: int,
    *,
    propagate_provider_failure: bool,
) -> KernelResult:
    if pending_native_messages:
        # Native APIs require a reply for every emitted tool-call id. Deliver
        # the final batch even when the turn budget stops further work.
        try:
            _transport._drain_native_budget(provider, pending_native_messages, native_tools)
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
    try:
        from codey.operations.completion_gate import evaluate as gate_evaluate
    except Exception:
        # Fail closed: a missing gate never completes.
        prompt = "Completion gate unavailable; cannot complete yet. Continue the task."
        try:
            pending = _transport._take_answered_reply(provider, reply, native, native_tools, prompt)
        except Exception as exc:
            return KernelResult(
                completed=False, summary=f"provider failed: {exc}",
                turns=int(session.turn or 0), stop_reason="provider_failure",
            )
        return prompt, pending
    try:
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
    if verdict.complete:
        # Native chains require every call id closed, including an accepted
        # done. Answer the done id with success before returning so the same
        # session can continue and the provider chain stays legal. A failed
        # receipt never counts as closed: surface provider failure / pending
        # delivery instead of claiming completion.
        if native and not isinstance(reply, str):
            try:
                ids = [str(getattr(c, "id", "") or "") for c in (getattr(reply, "tool_calls", ()) or [])]
                ids = [i for i in ids if i]
                if ids:
                    try:
                        _transport.call_provider_send_results(
                            provider,
                            [
                                {
                                    "role": "tool",
                                    "tool_call_id": i,
                                    "content": f"done accepted: {session.last_done_text[:500]}",
                                }
                                for i in ids
                            ],
                            native_tools,
                        )
                    except Exception as exc:
                        return KernelResult(
                            completed=False,
                            summary=f"provider failed delivering done receipt: {exc}",
                            turns=int(session.turn or 0),
                            stop_reason="provider_failure",
                        )
            except Exception as exc:
                return KernelResult(
                    completed=False,
                    summary=f"provider failed delivering done receipt: {exc}",
                    turns=int(session.turn or 0),
                    stop_reason="provider_failure",
                )
        return KernelResult(
            completed=True, summary=session.last_done_text, turns=int(session.turn or 0), stop_reason="done"
        )
    try:
        pending = _transport._take_answered_reply(provider, reply, native, native_tools, verdict.followup)
    except Exception as exc:
        return KernelResult(
            completed=False, summary=f"provider failed: {exc}",
            turns=int(session.turn or 0), stop_reason="provider_failure",
        )
    return verdict.followup, pending
