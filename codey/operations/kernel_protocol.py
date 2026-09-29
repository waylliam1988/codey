"""Single turn protocol for the unified kernel: JSON text and native calls.

Web JSON replies and native ``AssistantTurn`` values converge here into one
canonical ``ToolPlan`` under the same ``TaskPolicy`` and controller state.
Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from codey.runtime.core.models import Control, ToolCall, ToolPlan
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG as _ALIAS_ARGS

MAX_NATIVE_CALLS_PER_TURN = 8


@dataclass(frozen=True)
class TurnSnapshot:
    """One authoritative per-round observation: policy ∩ current facts.

    Built once at round start; any build failure raises and the kernel
    terminates the turn as controller_failure (fail-closed), never a stale
    fallback list. Carries the allowed tools plus both protocols' contracts
    for the round.
    """

    allowed: tuple[str, ...] | None
    contract_text: str
    native_tools: tuple[dict[str, Any], ...]


def controller_allowed_for_session(session: Any) -> tuple[str, ...] | None:
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


def _native_tools_for_policy(policy: Any, controller_allowed: Any = None) -> list[dict[str, Any]]:
    """Native schemas for one turn's snapshot; fail-closed like the names."""
    try:
        from codey.toolchain.tool_spec import native_tools_for_snapshot
    except Exception as exc:
        raise RuntimeError(f"native tool snapshot unavailable: {exc}") from exc
    return list(native_tools_for_snapshot(policy, controller_allowed))


def build_turn_snapshot(session: Any, *, native: bool = False) -> TurnSnapshot:
    """Build the round's authoritative snapshot; raises on failure."""
    # Fail closed: a snapshot computation failure never encodes as None
    # (unlimited). Callers must treat the exception as a per-turn config
    # error and stop.
    allowed = controller_allowed_for_session(session)
    from codey.toolchain.tool_spec import json_contract_text as _contract

    contract_now = _contract(session.policy, controller_allowed=allowed) if _contract is not None else ""
    native_now = _native_tools_for_policy(session.policy, allowed) if native else []
    return TurnSnapshot(
        allowed=allowed,
        contract_text=str(contract_now or ""),
        native_tools=tuple(native_now),
    )


_CONTROLLER_ALIASES = frozenset(_ALIAS_ARGS or {})


def _grant_for_tool(tool: str) -> str:
    try:
        from codey.toolchain.tool_spec import spec_for_tool
    except Exception:
        return ""
    try:
        spec = spec_for_tool(tool)
    except Exception:
        return ""
    if spec is None or not spec.grant:
        # Unknown tools are denied, never passed as control.
        return ""
    return spec.grant


def _canonical_name(tool: str) -> str:
    try:
        from codey.toolchain.tool_spec import canonical_tool_name
    except Exception:
        return str(tool or "").strip().lower()
    try:
        return canonical_tool_name(tool)
    except Exception:
        return str(tool or "").strip().lower()


def _is_coding_only_tool(tool: str) -> bool:
    name = _canonical_name(tool)
    if not name or name == "done":
        return False
    try:
        from codey.toolchain.tool_spec import spec_for_tool
    except Exception:
        return False
    try:
        spec = spec_for_tool(name)
    except Exception:
        return False
    return spec is not None and spec.executor == "project"


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
    source = str(text or "").strip()
    if source.startswith("```") and source.endswith("```"):
        first_newline = source.find("\n")
        if first_newline < 0:
            return []
        language = source[3:first_newline].strip().lower()
        if language not in {"", "json"}:
            return []
        source = source[first_newline + 1:-3].strip()
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
    if not spans:
        return []
    # Text mode is a canonical JSON protocol.  Do not mine an arbitrary
    # provider frame or prose for an executable object: every non-JSON byte
    # between the first and last object must be whitespace.
    first_start = spans[0][0]
    last_end = spans[-1][1]
    if source[:first_start].strip() or source[last_end:].strip():
        return []
    if any(source[end:start].strip() for (_, end), (start, _) in zip(spans, spans[1:], strict=False)):
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
    # The contract validator fills optional defaults for older callers. The
    # shared kernel retains only arguments the model actually supplied, so
    # restricted subflows can still reject explicitly forbidden fields.
    return {key: value for key, value in result.args.items() if key in args}, ""


def _validate_tool_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    name = _canonical_name(tool) or str(tool or "").strip().lower()
    # ToolSpec is the single authoritative definition: required-arg presence
    # is decided here so JSON/native schemas and validation cannot drift.
    # Project/research legacy validators are additional type-repair constraints
    # only; third-task (custom) tools pass on the generic spec alone.
    try:
        from codey.toolchain.tool_spec import spec_for_tool as _spec_for
        from codey.toolchain.tool_spec import validate_args_against_spec
    except Exception:
        _spec_for = None  # type: ignore[assignment]
        validate_args_against_spec = None  # type: ignore[assignment]
    executor = ""
    spec = None
    if _spec_for is not None:
        try:
            spec = _spec_for(name)
            executor = spec.executor if spec is not None else ""
        except Exception:
            executor = ""
            spec = None
        if spec is None:
            return {}, f"unknown tool: {tool or '?'}"
        if validate_args_against_spec is not None:
            try:
                spec_error = validate_args_against_spec(name, args if isinstance(args, dict) else {})
            except Exception as exc:
                # Fail closed: a broken validator never means "accept".
                detail = str(exc).strip()[:200] or type(exc).__name__
                return {}, f"{name} args invalid: spec validator failed ({detail})"
            if spec_error:
                return {}, spec_error
        else:
            # Spec validator unavailable: fail closed, never accept.
            return {}, f"{name} args invalid: spec validator unavailable"
        if name == "done":
            # ``done`` is a control tool with no runtime executor. Its
            # canonical ToolSpec validation above is the complete contract.
            return dict(args) if isinstance(args, dict) else {}, ""
        # Custom/third-task tools: generic spec validation is sufficient.
        # They run via injected executors; no legacy coding/research repair.
        if executor not in {"project", "source", "knowledge"}:
            # Controller aliases always lower via research validation.
            is_alias = name in set(_ALIAS_ARGS or {})
            if is_alias or (name == "source_search" and isinstance(args, dict) and "source_id" in args):
                return _validate_research_args(name, args if isinstance(args, dict) else {})
            return dict(args) if isinstance(args, dict) else {}, ""
    if name in _CONTROLLER_ALIASES or (isinstance(args, dict) and name == "source_search" and "source_id" in args):
        return _validate_research_args(name, args)
    if executor == "project":
        return _validate_coding_args(name, args)
    if executor in {"source", "knowledge"}:
        return _validate_research_args(name, args)
    # Fallback when the spec helper is unavailable (import failure): keep the
    # legacy split so validation still fails closed, never open.
    if name in _CONTROLLER_ALIASES:
        return _validate_research_args(name, args)
    # Spec exists but executor unknown (e.g. custom registered while legacy
    # helpers failed to import): generic pass-through on required-args only.
    if spec is not None:
        return dict(args) if isinstance(args, dict) else {}, ""
    return {}, f"unknown tool: {tool or '?'}"


def _plan_from_tool_objects(
    items: list[tuple[str, dict[str, Any], str]],
    *,
    policy: Any,
    controller_allowed: set[str] | None,
) -> ToolPlan:
    calls: list[ToolCall] = []
    for raw_tool, args, call_id in items:
        tool = _canonical_name(raw_tool)
        if not tool:
            return _invalid_plan(f"unknown tool: {raw_tool or '?'}", kind="unknown_tool", tool=str(raw_tool or ""))
        if tool == "done":
            if len(items) != 1:
                return _invalid_plan("done must be the only call in a turn", tool=tool, kind="too_many_tools")
            if not _policy_allows(policy, "done"):
                return _disallowed_plan(tool)
            if not _controller_allows("done", controller_allowed):
                return _disallowed_plan(tool, controller=True)
            validated, error = _validate_tool_args(tool, args)
            if error:
                return _invalid_plan(error, tool=tool)
            text = str(validated.get("summary") or "").strip()
            return ToolPlan(calls=[], control=Control(kind="done", body=text))
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


__all__ = [
    "MAX_NATIVE_CALLS_PER_TURN",
    "TurnSnapshot",
    "build_turn_snapshot",
    "controller_allowed_for_session",
    "normalize_turn",
]
