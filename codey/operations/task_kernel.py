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

from codey.operations.kernel_protocol import _CONTROLLER_ALIASES, _policy_allows, normalize_turn
from codey.operations.kernel_session import TaskSession, effect_id_for_call, turn_effect_id
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult

__all__ = [
    "KernelResult",
    "TaskSession",
    "apply_auto_plan",
    "controller_allowed_for_session",
    "effect_id_for_call",
    "execute_turn",
    "kernel_prompt_for_session",
    "normalize_turn",
    "policy_for_dispatch",
    "provider_uses_native",
    "resume_policy",
    "run_task_kernel",
    "turn_effect_id",
]


"""Turn protocol lives in kernel_protocol; session facts in kernel_session."""


"""Turn protocol lives in kernel_protocol; session facts in kernel_session."""


"""Session facts live in kernel_session; turn protocol in kernel_protocol."""


def _result_ok(name: str, result: ToolResult, *, exit_code: int | None = None) -> bool:
    if exit_code is not None:
        try:
            return int(exit_code) == 0
        except (TypeError, ValueError):
            return False
    text = str(result.model_text or "")
    return not (text.startswith("ERROR:") or text.startswith("SKIPPED:") or text.startswith("NEEDS_OPEN:"))


def _fake_run_ok(text: str) -> bool:
    import re as _re

    lowered = str(text or "").lower()
    for match in _re.finditer(r"(\d+)\s+failed", lowered):
        try:
            if int(match.group(1)) > 0:
                return False
        except (TypeError, ValueError):
            return False
    for match in _re.finditer(r"(\d+)\s+passed", lowered):
        try:
            if int(match.group(1)) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return "pass" in lowered or lowered.strip().endswith("ok")


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call=call, model_text=f"ERROR: {message}")


def _build_delegate(session: TaskSession, project_path: Any, tool_fns: Any, research_tools: Any) -> Any:
    if project_path is None and research_tools is None:
        return None
    try:
        from codey.operations.kernel_execution import ExecutionDelegate
    except Exception:
        return None
    try:
        return ExecutionDelegate(
            session=session, project_path=project_path, tool_fns=tool_fns,
            research_tools=research_tools,
        )
    except Exception:
        return None


_READ_ONLY_TOOLS = frozenset({
    "list_dir", "read_file", "grep", "find_references",
    "web_search", "knowledge_search", "knowledge_read",
})


def _skip_unsettled(intent_sink: Any, identity: str, name: str) -> bool:
    if intent_sink is None:
        return False
    try:
        return bool(intent_sink.has_unsettled(identity)) and name not in _READ_ONLY_TOOLS
    except Exception:
        return False


def _replay_settled_slot(
    session: TaskSession, identity: str, call: ToolCall, name: str, active_turn: int,
) -> ToolResult | None:
    if identity not in (session.executed or {}):
        return None
    # Same turn slot already settled (retry after crash before delivery):
    # answer without re-executing dangerous writes. Full results stay
    # process-local (never in the bounded payload).
    full = session._memory_results.get(identity)
    if full is not None:
        call_id = str(getattr(call, "call_id", "") or full.call.call_id or "")
        return ToolResult(
            call=ToolCall(name=full.call.name, args=dict(full.call.args), call_id=call_id),
            model_text=full.model_text,
        )
    record = session.executed[identity]
    call_id = str(getattr(call, "call_id", "") or record.get("call_id", ""))
    return ToolResult(
        call=ToolCall(name=str(record.get("name", "") or name),
                      args=dict(call.args if isinstance(call.args, dict) else {}),
                      call_id=call_id),
        model_text=f"ERROR: already settled in turn {active_turn}; see prior delivery"
        if not bool(record.get("ok", False))
        else str(record.get("excerpt", "") or ""),
    )


def _record_facts_for_result(
    session: TaskSession,
    call: ToolCall,
    result: ToolResult,
    *,
    ok: bool,
    opened_url: str = "",
    evidence_items: list[dict[str, str]] | None = None,
    exit_code: int | None = None,
) -> None:
    name = str(call.name or "").strip().lower()
    args = call.args if isinstance(call.args, dict) else {}
    text = str(result.model_text or "")
    if not ok:
        return
    if name == "web_search":
        query = args.get("query", "")
        session.record_search(str(query or ""))
        for rid, url in _search_result_rows(text):
            session.record_search_result(rid, url)
    elif name == "open_url":
        url = (opened_url or str(args.get("url", "") or "")).strip()
        if url:
            session.record_open(url)
    elif name in _CONTROLLER_ALIASES:
        url = (opened_url or "").strip()
        if not url:
            key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}[name]
            rid = str(args.get(key, "") or "").strip().lower()
            url = (session.search_results.get(rid, "") or session.source_ids.get(rid, "")).strip()
        if url:
            session.record_open(url)
    elif name == "knowledge_write":
        session.notes_saved += 1
        for item in evidence_items or ():
            url = str(item.get("source_url", "") or "").strip()
            excerpt = str(item.get("excerpt", "") or "").strip()
            if url and excerpt:
                session.record_evidence(url, excerpt)
    elif name == "edit":
        session.record_edit(str(args.get("path", "") or "file"))
    elif name == "run":
        command = str(args.get("command", "") or "")
        latest = max([0, *list(session.edited_files.values())]) if session.edited_files else 0
        if exit_code is not None:
            try:
                passed = int(exit_code) == 0
            except (TypeError, ValueError):
                passed = False
            session.record_verification(command, latest, passed, exit_code=exit_code)
        else:
            session.record_verification(command, latest, _fake_run_ok(text))
    if text:
        session.transcript_notes.append(f"{name}: {text[:500]}")


def _search_result_rows(text: str) -> list[tuple[str, str]]:
    import re as _re

    rows: list[tuple[str, str]] = []
    for match in _re.finditer(r"https?://[^\s)>\]]+", str(text or "")):
        rows.append((f"r{len(rows) + 1}", match.group(0).rstrip(".,;)]")))
        if len(rows) >= 8:
            break
    return rows


def execute_turn(
    session: TaskSession,
    calls: list[ToolCall],
    *,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    run_id: object = "",
    turn: object | None = None,
    tool_index_base: object = 0,
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    delivered: Mapping[str, ToolResult] | None = None,
    intent_sink: Any = None,
) -> list[ToolResult]:
    """Execute one turn; identity is run+turn+index with durable delivery first."""

    runnable = dict(executors or {})
    try:
        active_turn = int(turn) if turn is not None else int(session.turn or 0)
    except (TypeError, ValueError):
        active_turn = int(session.turn or 0)
    try:
        base_index = int(tool_index_base or 0)
    except (TypeError, ValueError):
        base_index = 0
    run_ref = str(run_id or "")
    delivered_map = dict(delivered or {})

    import contextlib as _contextlib

    def settle(identity: str, call: ToolCall, result: ToolResult, ok: bool) -> None:
        _settle_slot(session, identity, call, result, ok=ok)
        with _contextlib.suppress(Exception):
            session._memory_results[identity] = result
        if intent_sink is not None:
            with _contextlib.suppress(Exception):
                intent_sink.settle(identity, bool(ok))
    results: list[ToolResult] = []
    delegate = _build_delegate(session, project_path, tool_fns, research_tools)
    with _contextlib.suppress(Exception):
        if intent_sink is not None:
            intent_sink.begin_turn(
                [(turn_effect_id(run_ref or "adhoc", active_turn, base_index + offset), call)
                 for offset, call in enumerate(calls or [])],
                turn=active_turn,
            )
    for offset, call in enumerate(calls or []):
        name = str(getattr(call, "name", "") or "").strip().lower()
        identity = turn_effect_id(run_ref or "adhoc", active_turn, base_index + offset)
        if identity in delivered_map:
            results.append(delivered_map[identity])
            continue
        if _skip_unsettled(intent_sink, identity, name):
            result = _error_result(call, f"interrupted {name} not re-executed; see prior intent")
            settle(identity, call, result, ok=False)
            results.append(result)
            continue
        replayed = _replay_settled_slot(session, identity, call, name, active_turn)
        if replayed is not None:
            results.append(replayed)
            continue
        if not _policy_allows(session.policy, name):
            result = _error_result(call, f"disallowed tool for this task policy: {name or '?'}")
            settle(identity, call, result, ok=False)
            results.append(result)
            continue
        if delegate is not None and delegate.handles(name):
            result, ok, opened, evidence, exit_code = delegate.execute(call)
            settle(identity, call, result, ok=ok)
            _record_facts_for_result(session, call, result, ok=ok, opened_url=opened,
                                     evidence_items=evidence, exit_code=exit_code)
            results.append(result)
            continue
        fn = runnable.get(name)
        if fn is None:
            result = _error_result(call, f"unknown tool executor: {name or '?'}")
            settle(identity, call, result, ok=False)
            results.append(result)
            continue
        try:
            produced = fn(call)
        except Exception as exc:
            result = _error_result(call, str(exc) or "tool failed")
            settle(identity, call, result, ok=False)
            results.append(result)
            continue
        if isinstance(produced, ToolResult):
            result = produced
        elif isinstance(produced, str):
            result = ToolResult(call=call, model_text=produced)
        else:
            result = ToolResult(call=call, model_text=str(produced))
        ok = _result_ok(name, result)
        if name == "run":
            ok = _fake_run_ok(str(result.model_text or ""))
        _settle_slot(session, identity, call, result, ok=ok)
        _record_facts_for_result(session, call, result, ok=ok)
        results.append(result)
    return results


def _settle_slot(session: TaskSession, identity: str, call: ToolCall, result: ToolResult, *, ok: bool) -> None:
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        session.executed[identity] = {
            "name": str(result.call.name or ""),
            "ok": bool(ok),
            "call_id": str(result.call.call_id or getattr(call, "call_id", "") or ""),
            "excerpt": str(result.model_text or "")[:500],
        }


def _snapshot_names(policy: Any) -> tuple[str, ...]:
    try:
        from codey.toolchain.tool_spec import visible_tool_names
    except Exception:
        return ()
    try:
        return tuple(visible_tool_names(policy))
    except Exception:
        return ()


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
        return ("knowledge_search", "knowledge_read", "web_search", "open_url", "open_result",
                "reopen_source", "open_hit", "source_search", "knowledge_write", "done")
    return None


def _controller_for_session(session: TaskSession) -> tuple[str, ...] | None:
    try:
        return controller_allowed_for_session(session)
    except Exception:
        return None


def provider_uses_native(provider: Any, *, provider_id: object = "") -> bool:
    """Reuse the production native-tool decision; never bare hasattr checks."""

    try:
        from codey.research.native_bridge import use_native_provider
    except Exception:
        return False
    try:
        return bool(use_native_provider(provider, str(provider_id or "")))
    except Exception:
        return False


def kernel_prompt_for_session(
    session: TaskSession,
    *,
    user_task: str = "",
    contract_text: str = "",
) -> str:
    task_text = str(user_task or getattr(session, "task_text", "") or "").strip()
    handoff = str(getattr(session, "handoff", "") or "").strip()
    project = str(getattr(session, "project", "") or "").strip() or "-"
    names = ", ".join(_snapshot_names(getattr(session, "policy", None))) or "none"
    parts = [f"User task (verbatim):\n{task_text or '(no task text)'}\n",
             f"Project: {project}\nTask kind: {getattr(session, 'task_kind', '')}"]
    if handoff:
        parts.append(f"Handoff from prior work:\n{handoff}")
    parts.append(f"Visible tools: {names}")
    if contract_text:
        parts.append(f"Tool contract (use exactly these shapes):\n{contract_text}")
    parts.append("Reply with exactly one JSON tool call per turn, or done.")
    return "\n\n".join(parts)


def _repair_prompt(error: str) -> str:
    return (
        "Your previous reply was not a valid tool call "
        f"({(error or 'invalid').strip()}). Reply with exactly one JSON object "
        'using {"tool":"...","args":{...}} and no other text.'
    )


def _format_results(results: list[ToolResult]) -> str:
    if not results:
        return "[no tool output]\n\nContinue with the next single JSON tool call."
    blocks = []
    for result in results:
        label = str(getattr(result.call, "name", "") or "tool")
        blocks.append(f"[result: {label}]\n{result.model_text}".rstrip())
    return "\n\n".join(blocks) + "\n\nContinue with the next single JSON tool call, or done."


@dataclass(frozen=True)
class KernelResult:
    completed: bool
    summary: str
    turns: int
    stop_reason: str


def _is_native_provider(provider: Any, *, provider_id: object = "") -> bool:
    return provider_uses_native(provider, provider_id=provider_id)


def _native_tools_for_policy(policy: Any) -> list[dict[str, Any]]:
    try:
        from codey.toolchain.tool_spec import native_tools_for_policy
    except Exception:
        return []
    try:
        return list(native_tools_for_policy(policy))
    except Exception:
        return []


def run_task_kernel(
    session: TaskSession,
    *,
    provider: Any,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
    run_id: object = "",
    provider_id: object = "",
    project_path: Any = None,
    tool_fns: Any = None,
    research_tools: Any = None,
    user_task: object = "",
    stop_flag: Any = None,
    delivered: Mapping[str, ToolResult] | None = None,
) -> KernelResult:
    runnable = dict(executors or {})
    delivered_map = dict(delivered or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    try:
        from codey.toolchain.tool_spec import json_contract_text
    except Exception:
        json_contract_text = None  # type: ignore[assignment]
    try:
        contract_text = json_contract_text(session.policy) if json_contract_text is not None else ""
    except Exception:
        contract_text = ""
    prompt = kernel_prompt_for_session(session, user_task=str(user_task or ""), contract_text=contract_text)
    pending_reply: Any = None
    pending_native_messages: list[dict[str, Any]] | None = None
    native = _is_native_provider(provider, provider_id=provider_id)
    native_tools = _native_tools_for_policy(session.policy) if native else []
    turns_used = 0
    for turn in range(1, max_turns + 1):
        if stop_flag is not None:
            try:
                if bool(stop_flag.is_set()):
                    return KernelResult(completed=False, summary="stopped", turns=turns_used,
                                        stop_reason="stopped")
            except Exception:
                pass
        session.turn = turn
        turns_used = turn
        controller = _controller_for_session(session)
        try:
            if pending_reply is not None:
                reply = pending_reply
                pending_reply = None
            elif native and pending_native_messages is not None:
                reply = provider.send_tool_results(pending_native_messages, native_tools, timeout=None)
                pending_native_messages = None
            elif native:
                reply = provider.send_turn(prompt, native_tools, timeout=None)
            else:
                reply = provider.send(prompt, timeout=None)
        except Exception as exc:
            return KernelResult(completed=False, summary=f"provider failed: {exc}", turns=turns_used, stop_reason="provider_failure")
        plan = normalize_turn(reply, policy=session.policy, controller_allowed=controller)
        if plan.protocol_error:
            prompt = _repair_prompt(plan.protocol_error)
            if native and not isinstance(reply, str):
                # Keep the native chain legal: answer dangling call ids.
                ids = [str(getattr(c, "id", "") or "") for c in (getattr(reply, "tool_calls", ()) or [])]
                ids = [i for i in ids if i]
                if ids:
                    try:
                        pending_reply = provider.send_tool_results(
                            [{"role": "tool", "tool_call_id": i, "content": f"ERROR: {plan.protocol_error}"} for i in ids],
                            native_tools, timeout=None,
                        )
                    except Exception:
                        pending_reply = None
            continue
        if plan.control is not None and plan.control.kind == "done":
            done_outcome = _handle_done_reply(session, plan, provider, reply, native, native_tools)
            if done_outcome is not None:
                if isinstance(done_outcome, KernelResult):
                    return done_outcome
                prompt, pending_reply = done_outcome
                continue
        results = execute_turn(session, list(plan.calls or ()), executors=runnable, run_id=run_id,
                               turn=turn, project_path=project_path, tool_fns=tool_fns,
                               research_tools=research_tools, delivered=delivered_map or None)
        if native:
            messages = _native_tool_messages(results)
            if messages:
                pending_native_messages = messages
                prompt = ""
                continue
        prompt = _format_results(results)
    return KernelResult(completed=False, summary="max turns reached", turns=turns_used, stop_reason="max_turns")


def _handle_done_reply(
    session: TaskSession, plan: ToolPlan, provider: Any, reply: Any, native: bool, native_tools: Any,
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
        verdict = gate_evaluate(session, session.last_done_text)
    except Exception as exc:
        prompt = f"Completion check failed ({exc}); cannot complete yet. Continue the task."
        pending = _take_answered_reply(provider, reply, native, native_tools, prompt)
        return prompt, pending
    if verdict.complete:
        return KernelResult(completed=True, summary=session.last_done_text,
                            turns=int(session.turn or 0), stop_reason="done")
    pending = _take_answered_reply(provider, reply, native, native_tools, verdict.followup)
    return verdict.followup, pending


def _native_tool_messages(results: list[ToolResult]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for result in results:
        call_id = str(getattr(result.call, "call_id", "") or "")
        if not call_id:
            # JSON-originated calls in a native session have no chain id;
            # synthesize a follow-up prompt instead of a tool message.
            return []
        messages.append({"role": "tool", "tool_call_id": call_id, "content": str(result.model_text or "")})
    return messages


def _take_answered_reply(
    provider: Any, reply: Any, native: bool, native_tools: Any, followup: str,
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
        return provider.send_tool_results(
            [{"role": "tool", "tool_call_id": i, "content": f"ERROR: {followup}"} for i in ids],
            native_tools, timeout=None,
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


def resume_policy(stored: Any, incoming: Any) -> Any:
    """Recovery reuses the persisted policy; new requests never replace it."""

    if stored is not None and callable(getattr(stored, "allows", None)):
        return stored
    return incoming


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
