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
from typing import Any

from codey.operations.kernel_protocol import normalize_turn
from codey.operations.task_session import TaskSession, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult

__all__ = [
    "KernelResult",
    "TaskSession",
    "apply_auto_plan",
    "controller_allowed_for_session",
    "execute_turn",
    "kernel_prompt_for_session",
    "normalize_turn",
    "policy_for_dispatch",
    "provider_uses_native",
    "run_task_kernel",
    "turn_effect_id",
]

from codey.operations.kernel_execution import execute_turn


def _snapshot_names(policy: Any, controller_allowed: Any = None) -> tuple[str, ...]:
    """Per-turn tool snapshot; fail-closed on build failure (no fallback).

    A snapshot failure must terminate the turn (controller_failure), never
    fall back to a policy-scope list that would show the model tools the
    current turn cannot execute.
    """
    try:
        from codey.toolchain.tool_spec import visible_tool_names_for_snapshot
    except Exception as exc:
        raise RuntimeError(f"tool snapshot unavailable: {exc}") from exc
    return tuple(visible_tool_names_for_snapshot(policy, controller_allowed))


def controller_allowed_for_session(session: TaskSession) -> tuple[str, ...] | None:
    policy = getattr(session, "policy", None)
    if not bool(getattr(policy, "strict_research", False)):
        return None
    results = dict(getattr(session, "search_results", {}) or {})
    opened = set(getattr(session, "opened_sources", set()) or set())
    evidence = list(getattr(session, "evidence", []) or [])
    if not results and not opened:
        return ("knowledge_search", "knowledge_read", "web_search", "done")
    if not opened:
        return ("knowledge_search", "knowledge_read", "web_search", "open_url", "open_result", "done")
    if not evidence:
        return (
            "knowledge_search",
            "knowledge_read",
            "web_search",
            "open_url",
            "open_result",
            "reopen_source",
            "open_hit",
            "source_search",
            "knowledge_write",
            "done",
        )
    return None


def _controller_for_session(session: TaskSession) -> tuple[str, ...] | None:
    # Fail closed: snapshot computation failure never encodes as None (unlimited).
    # Callers must treat the exception as a per-turn config error and stop.
    return controller_allowed_for_session(session)


def provider_uses_native(provider: Any, *, provider_id: object = "") -> bool:
    """Reuse the production native-tool decision; never bare hasattr checks."""

    try:
        from codey.providers.native_tools import supports_native_tools
    except Exception:
        return False
    try:
        probe = getattr(provider, "provider", provider)
        return bool(supports_native_tools(probe, str(provider_id or "")))
    except Exception:
        return False


def kernel_prompt_for_session(
    session: TaskSession,
    *,
    user_task: str = "",
    contract_text: str = "",
    context_text: str = "",
    controller_allowed: Any = None,
    native: bool = False,
) -> str:
    task_text = str(user_task or getattr(session, "task_text", "") or "").strip()
    handoff = str(getattr(session, "handoff", "") or "").strip()
    project = str(getattr(session, "project", "") or "").strip() or "-"
    names = ", ".join(_snapshot_names(getattr(session, "policy", None), controller_allowed)) or "none"
    parts = [
        f"User task (verbatim):\n{task_text or '(no task text)'}\n",
        f"Project: {project}\nTask kind: {getattr(session, 'task_kind', '')}",
    ]
    if handoff:
        parts.append(f"Factual handoff from prior work:\n{handoff}")
    if context_text:
        parts.append(f"Project context:\n{context_text}")
    current = _coding_context_for_session(session)
    if current:
        parts.append(current)
    if bool(getattr(getattr(session, "policy", None), "strict_research", False)):
        parts.append(
            "Research evidence rules: search results are leads, not evidence. "
            "Use web_search, then open_result or open_url before citing a source. "
            "Save short exact excerpts with knowledge_write. Use only local tool "
            "results as evidence, including when a web model has built-in browsing. "
            "Finish with done and these report sections: 结论, 关键证据, 反证与限制, "
            "来源质量, 搜索覆盖, 来源. Cite only sources opened and saved in this run."
        )
    parts.append(f"Visible tools: {names}")
    if contract_text:
        if native:
            parts.append(f"Tool contract (use exactly these tools):\n{contract_text}")
        else:
            parts.append(f"Tool contract (use exactly these shapes):\n{contract_text}")
    if native:
        parts.append("Use the provided native tools for this turn; do not reply with raw JSON.")
    else:
        parts.append("Reply with exactly one JSON tool call per turn, or done.")
    return "\n\n".join(parts)


def _coding_context_for_session(session: TaskSession) -> str:
    if not bool(getattr(session, "coding_context_enabled", True)):
        return ""
    if getattr(session, "task_kind", "") not in {"project", "hybrid", "planning"}:
        return ""
    try:
        from codey.workspace.coding_context import CodingContext, render_coding_context

        return render_coding_context(
            CodingContext(
                read_files=tuple(sorted(getattr(session, "read_files", set()) or set())),
                edit_eligible_files=tuple(sorted(getattr(session, "read_files", set()) or set())),
                changed_files=tuple(sorted(getattr(session, "edited_files", {}).keys())),
                verification_fresh=bool(getattr(session, "verifications", ())),
            )
        )
    except Exception:
        return ""


def _repair_prompt(error: str) -> str:
    return (
        "Your previous reply was not a valid tool call "
        f"({(error or 'invalid').strip()}). Reply with exactly one JSON object "
        'using {"tool":"...","args":{...}} and no other text.'
    )


def _result_context(result: ToolResult, session: TaskSession) -> str:
    text = str(result.model_text or "")
    if result.call.name == "web_search" and session.search_results:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.search_results.items())[-12:])
        return f"{text}\n\nAvailable result IDs for open_result:\n{refs}"
    if result.call.name == "source_search" and session.hit_targets:
        refs = "\n".join(
            f"{key}: {target.get('url')} offset={target.get('offset')} pages={target.get('pages')}"
            for key, target in list(session.hit_targets.items())[-12:]
        )
        return f"{text}\n\nAvailable hit IDs for open_hit:\n{refs}"
    if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"} and session.source_ids:
        refs = "\n".join(f"{key}: {url}" for key, url in list(session.source_ids.items())[-12:])
        return f"{text}\n\nOpened source IDs for reopen_source and citations:\n{refs}"
    return text


def _format_results(results: list[ToolResult], session: TaskSession) -> str:
    if not results:
        return "[no tool output]\n\nContinue with the next single JSON tool call."
    blocks = []
    for result in results:
        label = str(getattr(result.call, "name", "") or "tool")
        blocks.append(f"[result: {label}]\n{_result_context(result, session)}".rstrip())
    return "\n\n".join(blocks) + "\n\nContinue with the next single JSON tool call, or done."


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str


def _is_native_provider(provider: Any, *, provider_id: object = "") -> bool:
    return provider_uses_native(provider, provider_id=provider_id)


def _native_tools_for_policy(policy: Any, controller_allowed: Any = None) -> list[dict[str, Any]]:
    """Native schemas for one turn's snapshot; fail-closed like _snapshot_names."""
    try:
        from codey.toolchain.tool_spec import native_tools_for_snapshot
    except Exception as exc:
        raise RuntimeError(f"native tool snapshot unavailable: {exc}") from exc
    return list(native_tools_for_snapshot(policy, controller_allowed))


def _emit_turn_event(on_event: Callable[[Any], None] | None, turn: int, reply: Any) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent

    display = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or "")
    on_event(RunEvent.turn_started(turn, str(display)))


def _event_call(session: TaskSession, call: ToolCall) -> ToolCall:
    name = str(call.name or "")
    project_name = {"list_dir": "ls", "read_file": "read", "grep": "search", "find_references": "references"}.get(name)
    if project_name is not None:
        return ToolCall(name=project_name, args=dict(call.args), call_id=call.call_id)
    key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}.get(name)
    if key is None:
        return call
    reference = str(call.args.get(key) or "").strip().lower()
    url = (
        (session.hit_targets.get(reference, {}).get("url") if name == "open_hit" else None)
        or session.search_results.get(reference)
        or session.source_ids.get(reference)
    )
    if not url:
        return call
    return ToolCall(name="open_url", args={"url": url}, call_id=call.call_id)


def _emit_tool_starts(
    on_event: Callable[[Any], None] | None, session: TaskSession, turn: int, calls: list[ToolCall]
) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent
    from codey.toolchain.definition import render_tool_activity

    for index, call in enumerate(calls):
        display_call = _event_call(session, call)
        on_event(RunEvent.tool_started(turn, display_call, render_tool_activity(display_call), index))


def _emit_tool_results(
    on_event: Callable[[Any], None] | None,
    session: TaskSession,
    results: list[ToolResult],
    *,
    run_id: object,
    turn: int,
) -> None:
    if on_event is None:
        return
    from codey.runtime.observe.events import RunEvent
    from codey.toolchain.runtime import ToolOutcome

    for index, result in enumerate(results):
        identity = turn_effect_id(str(run_id or "adhoc"), turn, index)
        record = session.executed.get(identity, {})
        ok = bool(record.get("ok", False))
        exit_code = None
        if isinstance(result.audit, dict):
            exit_code = result.audit.get("exit_code")
        if result.call.name == "run" and session.verifications:
            exit_code = exit_code if exit_code is not None else session.verifications[-1].get("exit_code")
        display = str(result.model_text or "")
        if result.call.name in {"open_url", "open_result", "open_hit", "reopen_source"}:
            display = next(
                (line.removeprefix("Title: ") for line in display.splitlines() if line.startswith("Title: ")),
                display.splitlines()[0] if display else "",
            )
        outcome = ToolOutcome(
            model_text=result.model_text,
            ok=ok,
            changed=bool(result.audit.get("changed", ok and result.call.name == "edit")),
            exit_code=exit_code,
            truncated=result.truncated,
            canonical=result.canonical,
            audit=result.audit,
            presentation={"result": display[:500], **dict(result.presentation)},
        )
        on_event(RunEvent.tool_finished(turn, _event_call(session, result.call), outcome, index))


def _provider_failure(exc: Exception, turns_used: int, *, propagate: bool) -> KernelResult:
    if propagate:
        raise exc
    return KernelResult(
        completed=False,
        summary=f"provider failed: {exc}",
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
) -> KernelResult | None:
    if on_shell_request is None or project_path is None:
        return None
    from codey.agents.shell_approval import ShellApprovalRequest, deferred_tool_call_from_call
    from codey.agents.tool_execution import evaluate_tool_call_policy_for, policy_asks_user

    for index, call in enumerate(calls):
        if call.name != "shell":
            continue
        decision, _replay = evaluate_tool_call_policy_for(
            call,
            project=project_path,
            permission_profile="coding_writer",
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
                    (turn_effect_id(str(run_id or "adhoc"), turn, offset), item, offset)
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
    controller_now = _controller_for_session(session)
    from codey.toolchain.tool_spec import json_contract_text as _contract

    contract_now = _contract(session.policy, controller_allowed=controller_now) if _contract is not None else ""
    native_now = _native_tools_for_policy(session.policy, controller_now) if native else []
    prompt_now = kernel_prompt_for_session(
        session,
        user_task=str(user_task or ""),
        contract_text=contract_now,
        context_text=context_text,
        controller_allowed=controller_now,
        native=native,
    )
    return controller_now, contract_now, native_now, prompt_now


def _apply_recovery_first(
    session: TaskSession,
    native: bool,
    pending_initial: list[ToolResult],
    prompt: str,
    pending_native_messages: list[dict[str, Any]] | None,
    *,
    provider_session_changed: bool = False,
) -> tuple[str, list[dict[str, Any]] | None]:
    """Deliver undelivered prior results before any new model call.

    Same native session may continue delivery with old call ids; a new
    provider or new session must only re-explain results as text so stale
    call ids are never handed to a different chain.
    """
    if not pending_initial:
        return prompt, pending_native_messages
    # Cross-provider / new-session recovery: text only, never old call ids.
    if provider_session_changed:
        try:
            return (
                "Continue the unfinished task using the latest local tool results below.\n\n"
                + _format_results(pending_initial, session)
            ), None
        except Exception:
            return prompt, pending_native_messages
    try:
        if native:
            recovered_messages = _native_tool_messages(pending_initial, session)
            if recovered_messages:
                return prompt, recovered_messages
            return (
                "Continue the unfinished task using the latest local tool results below.\n\n"
                + _format_results(pending_initial, session)
            ), pending_native_messages
        return (
            "Continue the unfinished task using the latest local tool results below.\n\n"
            + _format_results(pending_initial, session)
        ), pending_native_messages
    except Exception:
        return prompt, pending_native_messages


def _call_provider_send(provider: Any, prompt: str) -> Any:
    """Web send with backward compat for test doubles without timeout."""
    try:
        return provider.send(prompt, timeout=None)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return provider.send(prompt)


def _call_provider_send_turn(provider: Any, prompt: str, tools: Any) -> Any:
    try:
        return provider.send_turn(prompt, tools, timeout=None)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return provider.send_turn(prompt, tools)


def _call_provider_send_results(provider: Any, messages: Any, tools: Any) -> Any:
    try:
        return provider.send_tool_results(messages, tools, timeout=None)
    except TypeError as exc:
        if "timeout" not in str(exc):
            raise
        return provider.send_tool_results(messages, tools)


def _send_kernel_reply(
    provider: Any,
    native: bool,
    prompt: str,
    native_tools: Any,
    pending_reply: Any,
    pending_native_messages: list[dict[str, Any]] | None,
) -> tuple[Any, Any, list[dict[str, Any]] | None]:
    """One provider send; clears the consumed pending slot."""
    if pending_reply is not None:
        return pending_reply, None, pending_native_messages
    if native and pending_native_messages is not None:
        reply = _call_provider_send_results(provider, pending_native_messages, native_tools)
        return reply, None, None
    if native:
        return _call_provider_send_turn(provider, prompt, native_tools), None, pending_native_messages
    return _call_provider_send(provider, prompt), None, pending_native_messages


def _repair_native_dangling(
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    error: str,
) -> Any:
    """Answer dangling native call ids after a protocol error."""
    if not native or isinstance(reply, str):
        return None
    try:
        ids = [str(getattr(c, "id", "") or "") for c in (getattr(reply, "tool_calls", ()) or [])]
    except Exception:
        return None
    ids = [i for i in ids if i]
    if not ids:
        return None
    try:
        return _call_provider_send_results(
            provider,
            [{"role": "tool", "tool_call_id": i, "content": f"ERROR: {error}"} for i in ids],
            native_tools,
        )
    except Exception:
        return None


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
    prompt = _repair_prompt(plan.protocol_error)
    pending_reply = _repair_native_dangling(provider, reply, native, native_tools, plan.protocol_error)
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
) -> tuple[dict[str, Any], dict[str, ToolResult], int, bool, str, str, list[dict[str, Any]], str, Any, Any]:
    runnable = dict(executors or {})
    delivered_map = dict(delivered or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    pending_initial = list(initial_results or [])
    native = _is_native_provider(provider, provider_id=provider_id)
    identity_ref = f"{run_id}:{effect_scope}" if effect_scope else run_id
    _, initial_contract, initial_native, initial_prompt = _snapshot_for_turn_state(
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
        try:
            controller, turn_contract, turn_native_tools, turn_prompt = _snapshot_for_turn_state(
                session,
                native=native,
                user_task=user_task,
                context_text=context_text,
            )
        except Exception as exc:
            return KernelResult(
                completed=False,
                summary=f"controller configuration error: {exc}",
                turns=turns_used,
                stop_reason="controller_failure",
            )
        native_tools = turn_native_tools
        if turn == resume_start and pending_reply is None and pending_native_messages is None and not pending_initial:
            prompt = turn_prompt
            prev_contract = str(turn_contract or "")
        try:
            reply, pending_reply, pending_native_messages = _send_kernel_reply(
                provider,
                native,
                prompt,
                native_tools,
                pending_reply,
                pending_native_messages,
            )
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
        _emit_turn_event(on_event, turn, reply)
        plan = normalize_turn(reply, policy=session.policy, controller_allowed=controller)
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
        )
        if isinstance(done_action, KernelResult):
            return done_action
        if done_action is not None:
            prompt, pending_reply = done_action
            continue
        if approval is not None:
            return approval
        _emit_tool_starts(on_event, session, turn, calls)
        _outer_evidence = _outer_evidence_for_context(completion_context)
        results = execute_turn(
            session,
            calls,
            executors=runnable,
            run_id=run_id,
            effect_scope=effect_scope,
            turn=turn,
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
            execution_evidence=_outer_evidence,
        )
        _emit_tool_results(on_event, session, results, run_id=identity_ref, turn=turn)
        if native:
            messages = _native_tool_messages(results, session)
            if messages:
                pending_native_messages = messages
                prompt = ""
                # Native chains get the fresh schema every turn via send_turn;
                # still track contract drift for the next web fallback.
                try:
                    _, next_contract, _, _ = _snapshot_for_turn_state(
                        session,
                        native=native,
                        user_task=user_task,
                        context_text=context_text,
                    )
                    prev_contract = str(next_contract or "")
                except Exception:
                    pass
                continue
        base_prompt = _format_results(results, session)
        current_context = _coding_context_for_session(session)
        if current_context:
            base_prompt = f"{base_prompt}\n\n{current_context}"
        # Web text chains have no per-turn schema: when visible tools change,
        # resend the current tool contract plus the controller reason.
        try:
            _, next_contract, _, _ = _snapshot_for_turn_state(
                session,
                native=native,
                user_task=user_task,
                context_text=context_text,
            )
        except Exception:
            next_contract = prev_contract
        next_text = str(next_contract or "")
        if (not native) and next_text and next_text != prev_contract:
            names = ", ".join(_snapshot_names(session.policy, controller)) or "none"
            prompt = (
                f"Visible tools changed (controller state advanced): {names}\n"
                f"Tool contract (use exactly these shapes):\n{next_text}\n\n"
                f"{base_prompt}"
            )
            prev_contract = next_text
        else:
            prompt = base_prompt
    return _finish_after_budget(
        session,
        provider,
        pending_native_messages,
        native_tools,
        turns_used,
        propagate_provider_failure=propagate_provider_failure,
    )


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
        drained = _drain_native_budget(
            provider,
            pending_native_messages,
            native_tools,
            turns_used,
            propagate_provider_failure=propagate_provider_failure,
        )
        if drained is not None:
            return drained
    partial = str(getattr(session, "last_done_text", "") or "").strip()
    return KernelResult(
        completed=False, summary=partial or "max turns reached", turns=turns_used, stop_reason="max_turns"
    )


def _drain_native_budget(
    provider: Any,
    messages: list[dict[str, Any]],
    native_tools: Any,
    turns_used: int,
    *,
    propagate_provider_failure: bool,
) -> KernelResult | None:
    for _ in range(4):
        try:
            reply = _call_provider_send_results(provider, messages, native_tools)
        except Exception as exc:
            return _provider_failure(exc, turns_used, propagate=propagate_provider_failure)
        ids = [str(getattr(call, "id", "") or "") for call in (getattr(reply, "tool_calls", ()) or ())]
        ids = [item for item in ids if item]
        if not ids:
            return None
        messages = [
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "ERROR: turn budget exhausted; tool call was not executed",
            }
            for call_id in ids
        ]
    return KernelResult(False, "native tool chain exceeded budget drain limit", turns_used, "protocol")


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
        pending = _take_answered_reply(provider, reply, native, native_tools, prompt)
        return prompt, pending
    try:
        verdict = gate_evaluate(session, session.last_done_text, context=completion_context)
    except Exception as exc:
        prompt = f"Completion check failed ({exc}); cannot complete yet. Continue the task."
        pending = _take_answered_reply(provider, reply, native, native_tools, prompt)
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
                        _call_provider_send_results(
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
    pending = _take_answered_reply(provider, reply, native, native_tools, verdict.followup)
    return verdict.followup, pending


def _native_tool_messages(results: list[ToolResult], session: TaskSession) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for result in results:
        call_id = str(getattr(result.call, "call_id", "") or "")
        if not call_id:
            # JSON-originated calls in a native session have no chain id;
            # synthesize a follow-up prompt instead of a tool message.
            return []
        messages.append({"role": "tool", "tool_call_id": call_id, "content": _result_context(result, session)})
    return messages


def _take_answered_reply(
    provider: Any,
    reply: Any,
    native: bool,
    native_tools: Any,
    followup: str,
) -> Any:
    if not native or isinstance(reply, str):
        return None
    try:
        ids = [str(getattr(c, "id", "") or "") for c in (getattr(reply, "tool_calls", ()) or [])]
    except Exception:
        return None
    ids = [i for i in ids if i]
    if not ids:
        return None
    try:
        return _call_provider_send_results(
            provider,
            [{"role": "tool", "tool_call_id": i, "content": f"ERROR: {followup}"} for i in ids],
            native_tools,
        )
    except Exception:
        return None


def policy_for_dispatch(request: Any, kind: object, *, strict_research: object = False) -> Any:
    """Build the immutable TaskPolicy for one dispatch kind (user intent only)."""

    try:
        from codey.policies.task_policy import build_task_policy
    except Exception:
        return None
    try:
        return build_task_policy(request, task_kind=kind, strict_research=strict_research)
    except Exception:
        return None


def apply_auto_plan(policy: Any, plan_text: object) -> Any:
    """Narrow a policy by an auto PLAN; the plan can never widen grants."""

    if policy is None or not callable(getattr(policy, "allows", None)):
        return policy
    text = str(plan_text or "")
    lowered = text.lower()
    if "action:" not in lowered and "plan:" not in lowered:
        return policy
    try:
        from codey.policies.task_policy import TaskPolicy
    except Exception:
        return policy
    grants = set(getattr(policy, "grants", frozenset()) or frozenset())
    if "action: project" not in lowered and "action:project" not in lowered:
        grants.discard("project.write")
        grants.discard("shell.approval")
    if "action: research" not in lowered and "action:research" not in lowered:
        grants.discard("web.read")
        grants.discard("knowledge.read")
        grants.discard("knowledge.write")
        grants.discard("knowledge.link")
    grants.add("control")
    try:
        return TaskPolicy(
            grants=frozenset(grants),
            strict_research=bool(getattr(policy, "strict_research", False)),
            required_checks=tuple(getattr(policy, "required_checks", ()) or ()),
            source=str(getattr(policy, "source", "") or "") + ";auto_narrowed",
            version=int(getattr(policy, "version", 1) or 1),
        )
    except Exception:
        return policy
