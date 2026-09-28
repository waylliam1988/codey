"""One task tool loop for every task kind (operations layer).

Production topology: policy -> session -> snapshot -> adapter.send ->
normalize_turn -> done gate or execute_turn -> record -> deliver. Web text
JSON and native tool calls converge in ``normalize_turn``; project, web, and
knowledge tools dispatch through ``execute_turn``; ``done`` converges in the
single ``completion_gate``. The loop sends exactly one provider message per
iteration (no double-send): a ``done`` rejection becomes the next prompt,
tool results become the next prompt (web) or the next tool_results call
(native) returning the following reply.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from codey.protocols.done_compat import read_done_text
from codey.runtime.core.models import Control, ToolCall, ToolPlan, ToolResult
from codey.utils.refs import stable_ref

MAX_NATIVE_CALLS_PER_TURN = 8

_CODING_TOOLS = frozenset({
    "list_dir", "read_file", "read_files", "grep", "find_references",
    "parallel", "edit", "run", "shell", "done",
})
_RESEARCH_TOOLS = frozenset({
    "web_search", "open_url", "source_search", "knowledge_search",
    "knowledge_read", "knowledge_write", "knowledge_link", "done",
})
_CONTROLLER_ALIASES = frozenset({"open_result", "reopen_source", "open_hit"})


def _grant_for_tool(tool: str) -> str:
    try:
        from codey.policies.task_policy import CODING_TOOL_GRANTS, RESEARCH_TOOL_GRANTS
    except Exception:
        return "control"
    name = str(tool or "").strip().lower()
    if name in CODING_TOOL_GRANTS:
        return CODING_TOOL_GRANTS[name]
    if name in RESEARCH_TOOL_GRANTS:
        return RESEARCH_TOOL_GRANTS[name]
    return "control"


def _is_coding_only_tool(tool: str) -> bool:
    name = str(tool or "").strip().lower()
    return name in _CODING_TOOLS and name not in _RESEARCH_TOOLS and name != "done"


def _controller_allows(tool: str, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    name = str(tool or "").strip().lower()
    if _is_coding_only_tool(name):
        return True
    if name == "open_url":
        return bool({"open_url", *_CONTROLLER_ALIASES} & allowed)
    return name in allowed


def _policy_allows(policy: Any, tool: str) -> bool:
    allows = getattr(policy, "allows", None)
    if not callable(allows):
        return False
    try:
        return bool(allows(_grant_for_tool(tool)))
    except Exception:
        return False


def _disallowed_plan(tool: str, *, controller: bool = False) -> ToolPlan:
    if controller:
        message = f"{tool} is not allowed by the current controller state"
        kind = "disallowed_tool"
    else:
        message = f"disallowed tool for this task policy: {tool}"
        kind = "disallowed_tool"
    return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind,
                    protocol_tool_name=str(tool or ""))


def _invalid_plan(message: str, *, tool: str = "", kind: str = "invalid_args") -> ToolPlan:
    return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind,
                    protocol_tool_name=str(tool or ""))


def _extract_json_objects(text: str) -> list[dict[str, Any]]:
    try:
        from codey.protocols.json_scanner import balanced_json_spans
    except Exception:
        balanced_json_spans = None  # type: ignore[assignment]
    source = str(text or "")
    if balanced_json_spans is None:
        try:
            value = json.loads(source)
        except Exception:
            return []
        return [value] if isinstance(value, dict) else []
    objects: list[dict[str, Any]] = []
    try:
        spans = balanced_json_spans(source)
    except Exception:
        return []
    for start, end in spans:
        try:
            value = json.loads(source[start:end])
        except Exception:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _tool_and_args(obj: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = str(obj.get("tool", "") or obj.get("name", "") or "").strip().lower()
    raw_args = obj.get("args", None)
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    elif raw_args is None:
        args = {k: v for k, v in obj.items() if k not in {"tool", "name"}}
    else:
        args = {}
    return tool, args


def _validate_coding_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if tool in {"parallel", "read_files"}:
        return {}, f"{tool} batch calls are not supported in the unified kernel; call tools singly"
    try:
        from codey.toolchain import definition as tool_defs
        from codey.toolchain.runtime import MAX_REPLACEMENTS, READ_MAX_LINES
        from codey.toolchain.tool_args_repair import (
            ToolArgLimits,
            ToolArgsRepairError,
            normalize_tool_args,
        )
    except Exception as exc:
        return {}, f"coding validator unavailable: {exc}"
    spec = tool_defs.TOOL_DEFINITION_BY_NAME.get(tool)
    if spec is None or spec.runtime_name is None:
        return {}, f"unknown tool: {tool}"
    try:
        repair = normalize_tool_args(
            spec.runtime_name, dict(args),
            limits=ToolArgLimits(max_replacements=MAX_REPLACEMENTS, read_max_lines=READ_MAX_LINES),
        )
    except ToolArgsRepairError as exc:
        return {}, str(exc)
    except Exception as exc:
        return {}, f"{tool} args invalid: {exc}"
    return dict(repair.args), ""


def _validate_research_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if tool in _CONTROLLER_ALIASES:
        id_key = {"open_result": "result_id", "reopen_source": "source_id", "open_hit": "hit_id"}[tool]
        value = str(args.get(id_key, "") or "").strip()
        if not value:
            return {}, f"{tool} missing required arg '{id_key}'"
        return {id_key: value}, ""
    if tool == "source_search" and "source_id" in args:
        # Controller alias form: source_id resolves via controller state at
        # execution time; here only require a non-empty id and query.
        sid = str(args.get("source_id", "") or "").strip()
        query = str(args.get("query", "") or "").strip()
        if not sid:
            return {}, "source_search missing required arg 'source_id'"
        if not query:
            return {}, "source_search missing required arg 'query'"
        return {"source_id": sid, "query": query}, ""
    try:
        from codey.research.tool_contract import validate_tool_args
    except Exception as exc:
        return {}, f"research validator unavailable: {exc}"
    try:
        result = validate_tool_args(tool, dict(args))
    except Exception as exc:
        return {}, f"{tool} args invalid: {exc}"
    if not result.ok:
        return {}, result.error or f"{tool} args invalid"
    return dict(result.args), ""


def _validate_tool_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    name = str(tool or "").strip().lower()
    if name in _CONTROLLER_ALIASES or (name == "source_search" and "source_id" in args):
        return _validate_research_args(name, args)
    if name in _RESEARCH_TOOLS and name not in _CODING_TOOLS:
        return _validate_research_args(name, args)
    if name in _CODING_TOOLS:
        return _validate_coding_args(name, args)
    if not isinstance(args, dict):
        return {}, f"{tool} args must be an object"
    return dict(args), ""


def _plan_from_tool_objects(
    items: list[tuple[str, dict[str, Any], str]],
    *,
    policy: Any,
    controller_allowed: set[str] | None,
) -> ToolPlan:
    calls: list[ToolCall] = []
    for tool, args, call_id in items:
        if not tool:
            return _invalid_plan("no known tool in reply", kind="unknown_tool")
        if tool == "done":
            if len(items) != 1:
                return _invalid_plan("done must be the only call in a turn", tool=tool, kind="too_many_tools")
            if not _policy_allows(policy, "done"):
                return _disallowed_plan(tool)
            if not _controller_allows("done", controller_allowed):
                return _disallowed_plan(tool, controller=True)
            text = read_done_text(args)
            return ToolPlan(calls=[], control=Control(kind="done", body=text or "done"))
        if not _policy_allows(policy, tool):
            return _disallowed_plan(tool)
        if not _controller_allows(tool, controller_allowed):
            return _disallowed_plan(tool, controller=True)
        validated, error = _validate_tool_args(tool, args)
        if error:
            return _invalid_plan(error, tool=tool)
        calls.append(ToolCall(name=tool, args=validated, call_id=str(call_id or "")))
    if not calls:
        return _invalid_plan("no JSON tool call found", kind="no_json")
    body = "Need tool result" if len(calls) == 1 else "Need tool results"
    return ToolPlan(calls=calls, control=Control(kind="continue", body=body))


def normalize_turn(
    reply: str | object,
    *,
    policy: Any,
    controller_allowed: tuple[str, ...] | list[str] | set[str] | None = None,
) -> ToolPlan:
    allowed = None if controller_allowed is None else {str(n or "").strip().lower() for n in controller_allowed}
    tool_calls = getattr(reply, "tool_calls", None) if not isinstance(reply, str) else None
    if isinstance(reply, str) or tool_calls is None:
        text = reply if isinstance(reply, str) else str(getattr(reply, "text", "") or "")
        objects = _extract_json_objects(text)
        if not objects:
            folded = str(text or "").strip().lower()
            if not folded:
                return _invalid_plan("no JSON tool call found", kind="no_json")
            return _invalid_plan("no JSON tool call found", kind="no_json")
        items: list[tuple[str, dict[str, Any], str]] = []
        for obj in objects[:MAX_NATIVE_CALLS_PER_TURN]:
            if not isinstance(obj, dict):
                continue
            tool, args = _tool_and_args(obj)
            items.append((tool, args, ""))
            if len(items) >= MAX_NATIVE_CALLS_PER_TURN:
                break
        if len(objects) > MAX_NATIVE_CALLS_PER_TURN:
            return _invalid_plan(
                f"too many native tool calls in one turn ({len(objects)}); "
                f"send at most {MAX_NATIVE_CALLS_PER_TURN}",
                kind="too_many_tools",
            )
        return _plan_from_tool_objects(items, policy=policy, controller_allowed=allowed)
    calls = list(tool_calls or ())
    if len(calls) > MAX_NATIVE_CALLS_PER_TURN:
        return _invalid_plan(
            f"too many native tool calls in one turn ({len(calls)}); send at most {MAX_NATIVE_CALLS_PER_TURN}",
            kind="too_many_tools",
        )
    missing = [str(getattr(c, "name", "") or "?") for c in calls if not str(getattr(c, "id", "") or "")]
    if missing:
        return _invalid_plan(
            "native tool call without an id cannot be answered: " + ", ".join(missing),
            kind="invalid_args",
        )
    names = [str(getattr(c, "name", "") or "").strip().lower() for c in calls]
    if "done" in names and len(calls) != 1:
        return _invalid_plan("done must be the only call in a turn", tool="done", kind="too_many_tools")
    if not calls:
        text = str(getattr(reply, "text", "") or "")
        return normalize_turn(text, policy=policy, controller_allowed=controller_allowed)
    items = []
    for item in calls:
        name = str(getattr(item, "name", "") or "").strip().lower()
        args = getattr(item, "arguments", {})
        call_id = str(getattr(item, "id", "") or "")
        if not isinstance(args, dict):
            return _invalid_plan(f"{name} args must be an object", tool=name)
        items.append((name, dict(args), call_id))
    return _plan_from_tool_objects(items, policy=policy, controller_allowed=allowed)


def effect_id_for_call(call: ToolCall) -> str:
    try:
        args_json = json.dumps(call.args if isinstance(call.args, dict) else {}, sort_keys=True, ensure_ascii=False)
    except Exception:
        args_json = str(getattr(call, "args", {}))
    return stable_ref("task_effect", str(getattr(call, "name", "") or ""), args_json)


@dataclass
class TaskSession:
    policy: Any
    task_kind: str = "project"
    project: str = ""
    max_turns: int = 8
    searches: list[str] = field(default_factory=list)
    opened_sources: set[str] = field(default_factory=set)
    evidence: list[dict[str, str]] = field(default_factory=list)
    edited_files: dict[str, int] = field(default_factory=dict)
    verifications: list[dict[str, Any]] = field(default_factory=list)
    notes_saved: int = 0
    transcript_notes: list[str] = field(default_factory=list)
    last_done_text: str = ""
    turn: int = 0
    executed: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record_search(self, query: str) -> None:
        text = str(query or "").strip()
        if text:
            self.searches.append(text[:240])

    def record_open(self, url: str) -> None:
        text = str(url or "").strip()
        if text:
            self.opened_sources.add(text[:500])

    def record_evidence(self, source_url: str, excerpt: str) -> None:
        url = str(source_url or "").strip()
        clip = str(excerpt or "").strip()
        if url and clip:
            self.evidence.append({"source_url": url[:500], "excerpt": clip[:600]})

    def record_edit(self, path: str, revision: int | None = None) -> int:
        key = str(path or "").strip() or "file"
        try:
            rev = int(revision) if revision is not None else -1
        except (TypeError, ValueError):
            rev = -1
        if rev < 0:
            current = max([0, *list(self.edited_files.values())])
            rev = current + 1
        self.edited_files[key] = rev
        return rev

    def record_verification(self, command: str, revision: int, passed: bool) -> None:
        try:
            rev = int(revision)
        except (TypeError, ValueError):
            return
        self.verifications.append({"command": str(command or "")[:240], "revision": rev, "passed": bool(passed)})

    def notes_text(self) -> str:
        parts = [*self.transcript_notes]
        if self.last_done_text:
            parts.append(self.last_done_text)
        return "\n".join(parts)

    def to_payload(self) -> dict[str, Any]:
        try:
            policy_payload = self.policy.to_payload() if hasattr(self.policy, "to_payload") else {}
        except Exception:
            policy_payload = {}
        return {
            "task_kind": str(self.task_kind or ""),
            "project": str(self.project or ""),
            "max_turns": int(self.max_turns or 0),
            "searches": list(self.searches or []),
            "opened_sources": sorted(self.opened_sources or set()),
            "evidence": [dict(item) for item in (self.evidence or [])],
            "edited_files": dict(self.edited_files or {}),
            "verifications": [dict(item) for item in (self.verifications or [])],
            "notes_saved": int(self.notes_saved or 0),
            "transcript_notes": list(self.transcript_notes or [])[-50:],
            "last_done_text": str(self.last_done_text or ""),
            "turn": int(self.turn or 0),
            "executed": {str(k): dict(v) for k, v in (self.executed or {}).items()},
            "policy": policy_payload,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, Any] | None, *, policy: Any = None) -> TaskSession:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        active_policy = policy if policy is not None else data.get("policy")
        if not hasattr(active_policy, "allows"):
            try:
                from codey.policies.task_policy import TaskPolicy

                active_policy = TaskPolicy.from_payload(data.get("policy"))
            except Exception:
                active_policy = policy
        session = TaskSession(
            policy=active_policy,
            task_kind=str(data.get("task_kind", "") or "project"),
            project=str(data.get("project", "") or ""),
            max_turns=int(data.get("max_turns", 8) or 8),
        )
        try:
            session.searches = [str(i) for i in (data.get("searches", []) or []) if str(i)]
            session.opened_sources = {str(i) for i in (data.get("opened_sources", []) or []) if str(i)}
            session.evidence = [dict(i) for i in (data.get("evidence", []) or []) if isinstance(i, dict)]
            session.edited_files = {str(k): int(v) for k, v in (data.get("edited_files", {}) or {}).items()}
            session.verifications = [dict(i) for i in (data.get("verifications", []) or []) if isinstance(i, dict)]
            session.notes_saved = int(data.get("notes_saved", 0) or 0)
            session.transcript_notes = [str(i) for i in (data.get("transcript_notes", []) or [])]
            session.last_done_text = str(data.get("last_done_text", "") or "")
            session.turn = int(data.get("turn", 0) or 0)
            raw_executed = data.get("executed", {}) or {}
            session.executed = {str(k): dict(v) for k, v in raw_executed.items() if isinstance(v, dict)}
        except Exception:
            pass
        return session


def _stored_result(effect_id: str, session: TaskSession) -> ToolResult | None:
    record = (session.executed or {}).get(effect_id)
    if not isinstance(record, dict):
        return None
    try:
        call = ToolCall(
            name=str(record.get("name", "") or ""),
            args=dict(record.get("args", {}) or {}),
            call_id=str(record.get("call_id", "") or ""),
        )
        return ToolResult(call=call, model_text=str(record.get("model_text", "") or ""))
    except Exception:
        return None


def _store_result(session: TaskSession, effect_id: str, result: ToolResult) -> None:
    import contextlib as _contextlib

    with _contextlib.suppress(Exception):
        session.executed[effect_id] = {
            "name": str(result.call.name or ""),
            "args": dict(result.call.args or {}),
            "call_id": str(result.call.call_id or ""),
            "model_text": str(result.model_text or ""),
        }


def _error_result(call: ToolCall, message: str) -> ToolResult:
    return ToolResult(call=call, model_text=f"ERROR: {message}")


def _record_facts_for_result(session: TaskSession, call: ToolCall, result: ToolResult) -> None:
    name = str(call.name or "").strip().lower()
    args = call.args if isinstance(call.args, dict) else {}
    text = str(result.model_text or "")
    if name == "web_search":
        query = args.get("query", "")
        session.record_search(str(query or ""))
    elif name == "open_url":
        url = str(args.get("url", "") or "").strip()
        if url:
            session.record_open(url)
    elif name in _CONTROLLER_ALIASES:
        target = text.strip()[:500] or str(args.get("result_id", "") or args.get("source_id", "") or "")
        if target and "ERROR" not in text:
            session.record_open(target)
    elif name == "knowledge_write":
        session.notes_saved += 1
        sources = args.get("sources", [])
        evidence = args.get("evidence", [])
        if isinstance(sources, str):
            sources = [sources]
        if isinstance(evidence, dict):
            evidence = [evidence]
        saved = False
        if isinstance(evidence, list):
            for item in evidence:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("source_url", "") or item.get("source", "") or "")
                excerpt = str(item.get("excerpt", "") or item.get("claim", "") or "")
                if url and excerpt:
                    session.record_evidence(url, excerpt)
                    saved = True
        if not saved and isinstance(sources, list) and sources:
            session.record_evidence(str(sources[0]), text[:300] or "saved note")
        elif not saved:
            session.record_evidence("note:local", text[:300] or "saved note")
    elif name == "knowledge_link":
        session.notes_saved += 0
    elif name == "edit":
        path = str(args.get("path", "") or "file")
        if "ERROR" not in text:
            session.record_edit(path)
    elif name == "run":
        command = str(args.get("command", "") or "")
        latest = max([0, *list(session.edited_files.values())]) if session.edited_files else 0
        passed = "pass" in text.lower() or text.strip().endswith("ok")
        session.record_verification(command, latest, passed)
    if text and "ERROR" not in text:
        session.transcript_notes.append(f"{name}: {text[:500]}")


def execute_turn(
    session: TaskSession,
    calls: list[ToolCall],
    *,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
) -> list[ToolResult]:
    runnable = dict(executors or {})
    results: list[ToolResult] = []
    for call in calls or []:
        name = str(getattr(call, "name", "") or "").strip().lower()
        effect_id = effect_id_for_call(call)
        stored = _stored_result(effect_id, session)
        if stored is not None:
            # Recovery: reuse the stored observation instead of re-executing.
            # Keep the current call_id so native chains stay answerable.
            if str(getattr(call, "call_id", "") or "") and not stored.call.call_id:
                stored = ToolResult(
                    call=ToolCall(name=stored.call.name, args=dict(stored.call.args), call_id=str(call.call_id)),
                    model_text=stored.model_text,
                )
            results.append(stored)
            continue
        if not _policy_allows(session.policy, name):
            result = _error_result(call, f"disallowed tool for this task policy: {name or '?'}")
            _store_result(session, effect_id, result)
            results.append(result)
            continue
        fn = runnable.get(name)
        if fn is None:
            # Unknown executor in this kernel wiring: fail closed, still answer.
            result = _error_result(call, f"unknown tool executor: {name or '?'}")
            _store_result(session, effect_id, result)
            results.append(result)
            continue
        try:
            produced = fn(call)
        except Exception as exc:
            result = _error_result(call, str(exc) or "tool failed")
            _store_result(session, effect_id, result)
            _record_facts_for_result(session, call, result)
            results.append(result)
            continue
        if isinstance(produced, ToolResult):
            result = produced
        elif isinstance(produced, str):
            result = ToolResult(call=call, model_text=produced)
        else:
            result = ToolResult(call=call, model_text=str(produced))
        _store_result(session, effect_id, result)
        _record_facts_for_result(session, call, result)
        results.append(result)
    return results


def _snapshot_names(policy: Any) -> tuple[str, ...]:
    try:
        from codey.toolchain.registry import snapshot_for_policy
    except Exception:
        return ()
    try:
        return tuple(snapshot_for_policy(policy).names)
    except Exception:
        return ()


def _controller_for_session(session: TaskSession) -> tuple[str, ...] | None:
    policy = getattr(session, "policy", None)
    if not bool(getattr(policy, "strict_research", False)):
        return None
    opened = set(getattr(session, "opened_sources", set()) or set())
    evidence = list(getattr(session, "evidence", []) or [])
    if not opened:
        return ("knowledge_search", "knowledge_read", "web_search", "done")
    if not evidence:
        return ("knowledge_search", "knowledge_read", "web_search", "open_url", "open_result",
                "reopen_source", "open_hit", "source_search", "knowledge_write", "done")
    return None


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


def _is_native_provider(provider: Any) -> bool:
    return bool(callable(getattr(provider, "send_turn", None)) and callable(getattr(provider, "send_tool_results", None)))


def _native_tools_for_policy(policy: Any) -> list[dict[str, Any]]:
    names = set(_snapshot_names(policy))
    try:
        from codey.policies.task_policy import visible_research_tools
    except Exception:
        visible_research_tools = None  # type: ignore[assignment]
    if visible_research_tools is not None:
        try:
            for name in visible_research_tools(policy, None):
                names.add(name)
        except Exception:
            pass
    tools: list[dict[str, Any]] = []
    for name in sorted(names):
        tools.append({"type": "function", "function": {"name": name, "parameters": {"type": "object"}}})
    return tools


def run_task_kernel(
    session: TaskSession,
    *,
    provider: Any,
    executors: Mapping[str, Callable[[ToolCall], Any]] | None = None,
) -> KernelResult:
    runnable = dict(executors or {})
    max_turns = max(1, int(getattr(session, "max_turns", 8) or 8))
    names = _snapshot_names(session.policy)
    prompt = (
        f"Task kind: {session.task_kind}. Project: {session.project or '-'}. "
        f"Visible tools: {', '.join(names) or 'none'}. "
        "Reply with exactly one JSON tool call per turn, or done."
    )
    pending_reply: Any = None
    pending_native_messages: list[dict[str, Any]] | None = None
    native = _is_native_provider(provider)
    native_tools = _native_tools_for_policy(session.policy) if native else []
    turns_used = 0
    for turn in range(1, max_turns + 1):
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
            session.last_done_text = str(plan.control.body or "")
            try:
                from codey.operations.completion_gate import evaluate as gate_evaluate
            except Exception:
                return KernelResult(completed=True, summary=session.last_done_text or "done", turns=turns_used, stop_reason="done")
            verdict = gate_evaluate(session, session.last_done_text)
            if verdict.complete:
                return KernelResult(completed=True, summary=session.last_done_text, turns=turns_used, stop_reason="done")
            prompt = verdict.followup
            continue
        results = execute_turn(session, list(plan.calls or ()), executors=runnable)
        if native:
            messages: list[dict[str, Any]] = []
            for result in results:
                call_id = str(getattr(result.call, "call_id", "") or "")
                if not call_id:
                    # JSON-originated calls in a native session have no chain id;
                    # synthesize a follow-up prompt instead of a tool message.
                    messages = []
                    break
                messages.append({"role": "tool", "tool_call_id": call_id, "content": str(result.model_text or "")})
            if messages:
                pending_native_messages = messages
                prompt = ""
                continue
        prompt = _format_results(results)
    return KernelResult(completed=False, summary="max turns reached", turns=turns_used, stop_reason="max_turns")


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


__all__ = [
    "KernelResult",
    "TaskSession",
    "effect_id_for_call",
    "execute_turn",
    "normalize_turn",
    "policy_for_dispatch",
    "run_task_kernel",
]
