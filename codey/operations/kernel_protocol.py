"""Single turn protocol for the unified kernel: JSON text and native calls.

Web JSON replies and native ``AssistantTurn`` values converge here into one
canonical ``ToolPlan`` under the same ``TaskPolicy`` and controller state.
Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from codey.protocols.text_turn import DecodedTextTurn, TextAnswer, TextError, TextRequests, decode_text_turn
from codey.providers.base import AssistantTurn, TurnFinish
from codey.runtime.core.models import Control, ToolCall, ToolPlan
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG as _ALIAS_ARGS
from codey.toolchain.tool_spec import is_text_batch_tool

MAX_NATIVE_CALLS_PER_TURN = 8

if TYPE_CHECKING:
    from codey.operations.task_session import TaskSession


@dataclass(frozen=True)
class TurnSnapshot:
    """One authoritative per-round observation: policy ∩ current facts.

    Built once at round start; any build failure raises and the kernel
    terminates the turn as controller_failure (fail-closed), never a stale
    fallback list. Carries the allowed tools, frozen tool definitions plus
    both protocols' contracts for the round. The snapshot object贯穿发送、
    解析与执行：注册表变化下一轮生效，本轮解析与执行共用同一冻结定义。
    ``frozen_specs`` are nested-immutable (mapping proxies inside); the
    ``custom_executors`` table freezes third-task bindings so a mid-turn
    registry swap cannot reroute this turn's execution.
    """

    allowed: tuple[str, ...] | None
    contract_text: str
    tool_names: tuple[str, ...] = ()
    policy: Any = None
    frozen_specs: tuple[Any, ...] = ()
    custom_executors: tuple[tuple[str, Any], ...] = ()
    implementation_compatible: bool = True


@dataclass(frozen=True)
class ReceivedProviderTurn:
    """A received response with its original request's declarations and turn."""

    reply: AssistantTurn | str
    snapshot: TurnSnapshot
    prompt: str
    owner_session: TaskSession
    logical_turn: int = 1
    window_ref: Any = None
    effect_scope: str = ""


def controller_allowed_for_session(session: Any) -> tuple[str, ...] | None:
    policy = getattr(session, "policy", None)
    try:
        denied = {str(n or "").strip().lower() for n in (getattr(session, "controller_denied", ()) or ())
                  if str(n or "").strip()}
    except Exception:
        denied = set()
    if not bool(getattr(policy, "strict_research", False)):
        base: tuple[str, ...] | None = None
    else:
        results = dict(getattr(session, "search_results", {}) or {})
        opened = set(getattr(session, "opened_sources", set()) or set())
        evidence = list(getattr(session, "evidence", []) or [])
        if not results and not opened:
            base = ("knowledge_search", "knowledge_read", "web_search", "done")
        elif not opened:
            base = ("knowledge_search", "knowledge_read", "web_search", "open_url", "open_result", "done")
        elif not evidence:
            base = (
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
        else:
            base = None
    if base is not None:
        base = (*base, "ask_user", "read_tool_result")
    # Reference aliases require an observed target, in ordinary and strict
    # tasks alike. The resulting list drives both wire protocols and execution.
    for alias, facts in (("open_result", "search_results"),
                         ("reopen_source", "source_ids"), ("open_hit", "hit_targets")):
        if not getattr(session, facts, None):
            denied.add(alias)
    if not denied:
        return base
    if base is None:
        # 否决表存在时 unlimited 即不可用：按策略可见全集减去否决，
        # 收窄仍经同一快照进入提示、schema、解析与执行。
        from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

        base = tuple(visible_tool_names_for_snapshot(policy, None))
    return tuple(name for name in base if name not in denied)


def _frozen_specs_for_names(names: tuple[str, ...]) -> tuple[Any, ...]:
    """Capture immutable definitions for exactly the advertised names."""
    import copy as _copy

    try:
        from codey.toolchain.tool_spec import freeze_spec_parameters as _freeze
        from codey.toolchain.tool_spec import tool_specs as _all_specs
    except Exception as exc:
        raise RuntimeError(f"tool snapshot unavailable: {exc}") from exc
    specs = _all_specs()
    frozen: list[Any] = []
    for name in names:
        spec = specs.get(name)
        if spec is None:
            continue
        try:
            copied = _copy.deepcopy(spec)
        except Exception as exc:
            raise RuntimeError(f"tool snapshot freeze failed for {name}: {exc}") from exc
        try:
            frozen.append(_freeze(copied))
        except Exception as exc:
            raise RuntimeError(f"tool snapshot freeze failed for {name}: {exc}") from exc
    return tuple(frozen)


def _frozen_custom_executors(names: tuple[str, ...]) -> tuple[tuple[str, Any], ...]:
    """Freeze third-task executor bindings for this turn (same source)."""
    try:
        from codey.toolchain.tool_spec import custom_executor_for as _fn_for
    except Exception as exc:
        raise RuntimeError(f"tool snapshot executors unavailable: {exc}") from exc
    bindings: list[tuple[str, Any]] = []
    for name in names:
        try:
            fn = _fn_for(name)
        except Exception as exc:
            raise RuntimeError(f"tool snapshot executor freeze failed for {name}: {exc}") from exc
        bindings.append((name, fn))
    return tuple(bindings)


def frozen_spec_map(snapshot: TurnSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    mapping = {spec.name: spec for spec in snapshot.frozen_specs}
    if set(mapping) != set(snapshot.tool_names):
        raise ValueError("incomplete tool snapshot definitions")
    return mapping


def frozen_custom_executor_map(snapshot: TurnSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    mapping = dict(snapshot.custom_executors)
    if set(mapping) != set(snapshot.tool_names):
        raise ValueError("incomplete tool snapshot executor bindings")
    return mapping


def build_turn_snapshot(session: Any, *, native: bool = False) -> TurnSnapshot:
    """Build the round's authoritative snapshot; raises on failure."""
    # Fail closed: a snapshot computation failure never encodes as None
    # (unlimited). Callers must treat the exception as a per-turn config
    # error and stop.
    allowed = controller_allowed_for_session(session)
    from codey.toolchain.tool_spec import visible_tool_names_for_snapshot

    names = tuple(visible_tool_names_for_snapshot(session.policy, allowed, native=native))
    frozen = _frozen_specs_for_names(names)
    if "codemode" in names:
        from codey.codemode.catalog import build_codemode_catalog, render_declarations

        catalog = build_codemode_catalog(frozen, allow_writes=session.policy.allows("project.write"))
        declarations = render_declarations(catalog)
        frozen = tuple(replace(spec, declarations=declarations)
                       if spec.name == "codemode" else spec for spec in frozen)
    from codey.toolchain.tool_spec import json_contract_text as _contract

    # JSON 与 native 契约同源：都从同一冻结定义生成，不再查实时注册表。
    contract_now = _contract(frozen)
    executors = _frozen_custom_executors(names)
    return TurnSnapshot(
        allowed=allowed,
        contract_text=contract_now,
        tool_names=names,
        policy=session.policy,
        frozen_specs=frozen,
        custom_executors=executors,
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
        return (tool or "").strip().lower()
    try:
        return canonical_tool_name(tool)
    except Exception:
        return (tool or "").strip().lower()


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
    name = (tool or "").strip().lower()
    if _is_coding_only_tool(name):
        return True
    if name == "open_url":
        return bool({"open_url", *_CONTROLLER_ALIASES} & allowed)
    return name in allowed


def _policy_allows(policy: Any, tool: str, frozen_specs: dict[str, Any] | None = None) -> bool:
    allows = getattr(policy, "allows", None)
    if not callable(allows):
        return False
    try:
        spec = frozen_specs.get(tool) if frozen_specs is not None else None
        grant = spec.grant if spec is not None else "" if frozen_specs is not None else _grant_for_tool(tool)
        return bool(grant and allows(grant))
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
                    protocol_tool_name=(tool or ""))


def _invalid_plan(message: str, *, tool: str = "", kind: str = "invalid_args") -> ToolPlan:
    return ToolPlan(calls=[], control=None, protocol_error=message, protocol_error_kind=kind,
                    protocol_tool_name=(tool or ""))


def _tool_and_args(obj: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    tool = str(obj.get("tool", "") or "").strip().lower()
    raw_args = obj.get("args", None)
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    elif raw_args is None:
        args = {k: v for k, v in obj.items() if k != "tool"}
    else:
        args = dict[str, object]()
    return tool, args


def _validate_coding_args(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    if (tool or "").strip().lower() == "edit" and isinstance(args, dict):
        # Top-level old/new fields are not part of the canonical edit shape.
        if "old_string" in args or "new_string" in args:
            return {}, "edit requires replacements for existing files"
        replacements = args.get("replacements")
        if isinstance(replacements, list):
            for item in replacements:
                if isinstance(item, dict) and ("search" in item or "replace" in item):
                    return {}, "edit rejects legacy replacement fields: use old_string/new_string"
        for legacy_top in ("search", "replace"):
            if legacy_top in args:
                return {}, f"edit rejects legacy alias: {legacy_top}"
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


def _lookup_spec(name: str, frozen_specs: dict[str, Any] | None) -> tuple[Any, str]:
    try:
        if frozen_specs is not None:
            spec = frozen_specs.get(name)
            return spec, (spec.executor if spec is not None else "")
        from codey.toolchain.tool_spec import spec_for_tool as _spec_for

        spec = _spec_for(name)
        return spec, (spec.executor if spec is not None else "")
    except Exception:
        return None, ""


def _spec_error_for(name: str, args: dict[str, Any], spec: Any, frozen_specs: dict[str, Any] | None) -> str:
    payload = args if isinstance(args, dict) else dict[str, object]()
    try:
        if frozen_specs is not None:
            from codey.toolchain.tool_spec import validate_args_with_spec as _frozen_validate

            return _frozen_validate(spec, payload) or ""
        from codey.toolchain.tool_spec import validate_args_against_spec as _live_validate

        return _live_validate(name, payload) or ""
    except Exception as exc:
        detail = str(exc).strip()[:200] or type(exc).__name__
        return f"{name} args invalid: spec validator failed ({detail})"


def _validate_tool_args(
    tool: str, args: dict[str, Any], *, frozen_specs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    name = _canonical_name(tool) or (tool or "").strip().lower()
    # ToolSpec is the single authoritative definition: required-arg presence
    # is decided here so JSON/native schemas and validation cannot drift.
    # Project/research legacy validators are additional type-repair constraints
    # only; third-task (custom) tools pass on the generic spec alone.
    spec, executor = _lookup_spec(name, frozen_specs)
    if spec is None:
        return {}, f"unknown tool: {tool or '?'}"
    spec_error = _spec_error_for(name, args if isinstance(args, dict) else {}, spec, frozen_specs)
    if spec_error:
        return {}, spec_error
    if name == "ask_user":
        from codey.task.questions import question_args
        try:
            return question_args(args), ""
        except ValueError as exc:
            return {}, str(exc)
    if name == "done":
        # ``done`` is a control tool with no runtime executor. Its
        # canonical ToolSpec validation above is the complete contract.
        from codey.research.tool_contract import validate_tool_args

        validated_done = validate_tool_args("done", args)
        return (dict(validated_done.args), "") if validated_done.ok else ({}, validated_done.error)
    if name == "codemode":
        try:
            from codey.codemode.protocol import parse_codemode_args

            program = parse_codemode_args(args if isinstance(args, dict) else {})
            from codey.runtime.core.models import canonical_json_mapping

            return {"code": program.code, "language": program.language,
                    "inputs": canonical_json_mapping(program.inputs)}, ""
        except ValueError as exc:
            return {}, str(exc)
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
    return {}, "unsupported tool executor"


def _expand_text_batch(tool: str, args: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any], str]], str]:
    """Lower bounded read-only wrappers; validate the whole batch before effects."""
    from codey.toolchain.definition import MAX_PARALLEL_CALLS, TOOL_DEFINITION_BY_NAME

    if tool == "read_files":
        paths = args.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list) or not paths or len(paths) > MAX_NATIVE_CALLS_PER_TURN:
            return [], f"read_files requires 1..{MAX_NATIVE_CALLS_PER_TURN} paths"
        if any(not isinstance(path, str) or not path.strip() for path in paths):
            return [], "read_files paths must be non-empty strings"
        return [("read_file", {"path": path}, "") for path in paths], ""
    calls = args.get("calls")
    if not isinstance(calls, list) or not calls or len(calls) > MAX_PARALLEL_CALLS:
        return [], f"parallel requires 1..{MAX_PARALLEL_CALLS} read-only calls"
    expanded = []
    for row in calls:
        if not isinstance(row, dict):
            return [], "every parallel call must be an object"
        name, child_args = _tool_and_args(row)
        definition = TOOL_DEFINITION_BY_NAME.get(name)
        if definition is None or not definition.parallel_safe:
            return [], "parallel accepts only list_dir, read_file, and grep"
        if "args" in row and not isinstance(row["args"], dict):
            return [], f"{name} args must be an object"
        expanded.append((name, child_args, ""))
    return expanded, ""


def _lower_text_batches(items: list[tuple[str, dict[str, Any], str]]) -> tuple[list[tuple[str, dict[str, Any], str]], str]:
    expanded = []
    for raw_tool, args, call_id in items:
        name = _canonical_name(raw_tool)
        if is_text_batch_tool(name):
            if call_id:
                return [], "batch wrappers are text-only; native calls require individual ids"
            from codey.toolchain.tool_spec import validate_args_against_spec

            error = validate_args_against_spec(name, args)
            if error:
                return [], error
            children, error = _expand_text_batch(name, args)
            if error:
                return [], error
            expanded.extend(children)
        else:
            expanded.append((raw_tool, args, call_id))
    if len(expanded) > MAX_NATIVE_CALLS_PER_TURN:
        return [], "expanded batch exceeds the turn call limit"
    return expanded, ""


def _plan_from_tool_objects(
    items: list[tuple[str, dict[str, Any], str]],
    *,
    policy: Any,
    controller_allowed: set[str] | None,
    snapshot_names: tuple[str, ...] | None = None,
    frozen_specs: dict[str, Any] | None = None,
) -> ToolPlan:
    items, batch_error = _lower_text_batches(items)
    if batch_error:
        return _invalid_plan(batch_error)
    if sum(1 for raw_tool, _args, _call_id in items if _canonical_name(raw_tool) == "shell") and len(items) != 1:
        return _invalid_plan("shell must be the only tool call in a turn", kind="mixed_shell_batch", tool="shell")
    calls: list[ToolCall] = []
    text_keys: set[str] = set()
    for raw_tool, args, call_id in items:
        tool = _canonical_name(raw_tool)
        if not tool:
            return _invalid_plan(f"unknown tool: {raw_tool or '?'}", kind="unknown_tool", tool=(raw_tool or ""))
        if snapshot_names is not None and tool not in snapshot_names:
            return _invalid_plan(
                f"{tool} is not allowed by the current controller state (not in the turn snapshot: {tool})",
                kind="snapshot_tool_mismatch",
                tool=tool,
            )
        if tool in {"done", "ask_user"}:
            if len(items) != 1:
                return _invalid_plan(f"{tool} must be the only call in a turn", tool=tool, kind="too_many_tools")
            if not _policy_allows(policy, tool, frozen_specs):
                return _disallowed_plan(tool)
            if not _controller_allows(tool, controller_allowed):
                return _disallowed_plan(tool, controller=True)
            validated, error = _validate_tool_args(tool, args, frozen_specs=frozen_specs)
            if error:
                return _invalid_plan(error, tool=tool)
            text = str(validated.get("summary" if tool == "done" else "question") or "").strip()
            if tool == "done" and "report_sections" in validated:
                from codey.reviews.report_sections import render_done_report

                text = render_done_report(text, validated["report_sections"])
            return ToolPlan(calls=[], control=Control(kind=tool, body=text), control_args=validated)
        if not _policy_allows(policy, tool, frozen_specs):
            return _disallowed_plan(tool)
        if not _controller_allows(tool, controller_allowed):
            return _disallowed_plan(tool, controller=True)
        validated, error = _validate_tool_args(tool, args, frozen_specs=frozen_specs)
        if error:
            return _invalid_plan(error, tool=tool)
        if not call_id:
            key = json.dumps([tool, validated], sort_keys=True, ensure_ascii=False)
            if key in text_keys:
                continue
            text_keys.add(key)
        calls.append(ToolCall(name=tool, args=validated, call_id=(call_id or "")))
    if not calls:
        return _invalid_plan("no JSON tool call found", kind="no_json")
    body = "Need tool result" if len(calls) == 1 else "Need tool results"
    return ToolPlan(calls=calls, control=Control(kind="continue", body=body))


def _native_tool_objects(calls: list[Any]) -> list[tuple[str, dict[str, Any], str]] | ToolPlan:
    """Unwrap native calls without losing or inventing provider call ids."""
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
    ids = [getattr(call, "id", "") for call in calls]
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in ids):
        return _invalid_plan("native tool call ids must be nonempty strings of at most 256 characters", kind="invalid_args")
    if len(ids) != len(set(ids)):
        return _invalid_plan("duplicate native tool call ids cannot be answered unambiguously", kind="invalid_args")
    names = [str(getattr(c, "name", "") or "").strip().lower() for c in calls]
    if "done" in names and len(calls) != 1:
        return _invalid_plan("done must be the only call in a turn", tool="done", kind="too_many_tools")
    items = []
    for item in calls:
        name = str(getattr(item, "name", "") or "").strip().lower()
        args = getattr(item, "arguments", {})
        call_id = str(getattr(item, "id", "") or "")
        if not isinstance(args, dict):
            return _invalid_plan(f"{name} args must be an object", tool=name)
        items.append((name, dict(args), call_id))
    return items


def _decoded_text_objects(
    decoded: TextRequests | TextAnswer | TextError,
) -> list[tuple[str, dict[str, Any], str]] | ToolPlan:
    if isinstance(decoded, TextError):
        return _invalid_plan(decoded.message, tool=decoded.tool, kind=decoded.kind)
    if isinstance(decoded, TextAnswer):
        return ToolPlan(calls=[], control=Control(kind="done", body=decoded.text))
    return [(item.tool, dict(item.args), "") for item in decoded.items]


def _response_objects(reply: DecodedTextTurn | AssistantTurn) -> list[tuple[str, dict[str, Any], str]] | ToolPlan:
    if isinstance(reply, (TextRequests, TextAnswer, TextError)):
        return _decoded_text_objects(reply)
    if isinstance(reply, AssistantTurn):
        if reply.finish is TurnFinish.PROTOCOL_ERROR:
            detail = reply.raw.get("protocol_error")
            tool = reply.raw.get("protocol_tool_name")
            return _invalid_plan(detail[:500] if isinstance(detail, str) and detail else "provider returned malformed tool calls",
                                 tool=tool[:80] if isinstance(tool, str) else "", kind="invalid_args")
        if reply.finish is not TurnFinish.COMPLETE:
            return _invalid_plan("assistant response was truncated", kind="output_limit")
        if not reply.tool_calls:
            if reply.raw.get("text_protocol") is True:
                return _decoded_text_objects(decode_text_turn(reply.text))
            if not reply.text.strip():
                return _invalid_plan("empty assistant answer", kind="empty_answer")
            return ToolPlan(calls=[], control=Control(kind="done", body=reply.text.strip()))
    tool_calls = getattr(reply, "tool_calls", None) if not isinstance(reply, str) else None
    if tool_calls:
        return _native_tool_objects(list(tool_calls))
    if isinstance(reply, str):
        return _invalid_plan(
            "raw text must be decoded by the text protocol before entering the kernel",
            kind="raw_text",
        )
    return _invalid_plan("unsupported provider reply type", kind="invalid_args")


def normalize_turn(
    reply: DecodedTextTurn | AssistantTurn,
    *,
    policy: Any = None,
    controller_allowed: tuple[str, ...] | list[str] | set[str] | None = None,
    snapshot_names: tuple[str, ...] | None = None,
    snapshot: TurnSnapshot | None = None,
) -> ToolPlan:
    frozen: dict[str, Any] | None = None
    if snapshot is not None:
        try:
            if policy is not None and policy != snapshot.policy:
                return _invalid_plan("turn policy differs from the captured authorization")
            policy = snapshot.policy
            allowed_raw = getattr(snapshot, "allowed", None)
            allowed = None if allowed_raw is None else {str(n or "").strip().lower() for n in allowed_raw}
            snapshot_names = tuple(getattr(snapshot, "tool_names", ()) or ())
            frozen = frozen_spec_map(snapshot)
        except Exception as exc:
            return _invalid_plan(f"invalid tool snapshot: {exc}")
    else:
        allowed = None if controller_allowed is None else {(n or "").strip().lower() for n in controller_allowed}
    items = _response_objects(reply)
    if isinstance(items, ToolPlan):
        text_protocol = isinstance(reply, TextAnswer) or (
            isinstance(reply, AssistantTurn) and reply.raw.get("text_protocol") is True)
        body = items.control.body if items.control is not None else ""
        if (text_protocol and items.control is not None and items.control.kind == "done" and policy is not None
                and policy.allows("knowledge.write") and not body.lstrip().startswith(("{", "["))
                and re.search(r'"tool"\s*:\s*"knowledge_(?:write|commit)"', body)):
            return _invalid_plan(
                "A knowledge tool request embedded in prose or a code fence was not executed. "
                "To save knowledge, send one bare tool request and wait for its result. "
                "If this is intentionally an example, deliver it explicitly inside done.summary; "
                "do not claim that it was saved.", kind="embedded_tool_request")
        return items
    return _plan_from_tool_objects(
        items, policy=policy, controller_allowed=allowed,
        snapshot_names=snapshot_names, frozen_specs=frozen,
    )


__all__ = [
    "MAX_NATIVE_CALLS_PER_TURN",
    "TurnSnapshot",
    "build_turn_snapshot",
    "controller_allowed_for_session",
    "frozen_custom_executor_map",
    "frozen_spec_map",
    "normalize_turn",
]
