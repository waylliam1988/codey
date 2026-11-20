"""Tool argument validation and authorization for canonical kernel requests."""
from __future__ import annotations

from collections.abc import Mapping

from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolPlan
from codey.toolchain.tool_spec import _CONTROLLER_ALIAS_ID_ARG as _ALIAS_ARGS
from codey.toolchain.tool_spec import SOURCE_READ_TOOL_NAMES, ToolSpec
from codey.types.json_value import JsonValue, ToolArgs, decode_json_object

_CONTROLLER_ALIASES = frozenset(_ALIAS_ARGS)


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
        name = canonical_tool_name(tool)
        return name
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
        return bool(SOURCE_READ_TOOL_NAMES & allowed)
    return name in allowed


def _policy_allows(policy: TaskPolicy | None, tool: str, frozen_specs: Mapping[str, ToolSpec] | None = None) -> bool:
    if policy is None:
        return False
    try:
        spec = frozen_specs.get(tool) if frozen_specs is not None else None
        grant = spec.grant if spec is not None else "" if frozen_specs is not None else _grant_for_tool(tool)
        return bool(grant and policy.allows(grant))
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


def _tool_and_args(obj: Mapping[str, JsonValue]) -> tuple[str, ToolArgs]:
    tool = str(obj.get("tool", "") or "").strip().lower()
    raw_args = obj.get("args", None)
    args: ToolArgs
    if isinstance(raw_args, dict):
        args = dict(raw_args)
    elif raw_args is None:
        args = {k: v for k, v in obj.items() if k != "tool"}
    else:
        args = {}
    return tool, args


def _validate_coding_args(tool: str, args: ToolArgs) -> tuple[ToolArgs, str]:
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
    return decode_json_object(repair.args), ""


def _validate_research_args(tool: str, args: ToolArgs) -> tuple[ToolArgs, str]:
    if tool in _CONTROLLER_ALIASES:
        id_key = _ALIAS_ARGS[tool][1]
        value = str(args.get(id_key, "") or "").strip()
        if not value:
            return {}, f"{tool} missing required arg '{id_key}'"
        return {**args, id_key: value}, ""
    if tool == "source_search" and "source_id" in args:
        # Controller alias form: source_id resolves via controller state at
        # execution time; here only require a non-empty id and query.
        sid = str(args.get("source_id", "") or "").strip()
        query = str(args.get("query", "") or "").strip()
        if not sid:
            return {}, "source_search missing required arg 'source_id'"
        if not query:
            return {}, "source_search missing required arg 'query'"
        return {**args, "source_id": sid, "query": query}, ""
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
    return decode_json_object({key: value for key, value in result.args.items() if key in args}), ""


def _lookup_spec(name: str, frozen_specs: Mapping[str, ToolSpec] | None) -> tuple[ToolSpec | None, str]:
    try:
        if frozen_specs is not None:
            spec = frozen_specs.get(name)
            return spec, (spec.executor if spec is not None else "")
        from codey.toolchain.tool_spec import spec_for_tool as _spec_for

        spec = _spec_for(name)
        return spec, (spec.executor if spec is not None else "")
    except Exception:
        return None, ""


def _spec_error_for(
    name: str,
    args: ToolArgs,
    spec: ToolSpec,
    frozen_specs: Mapping[str, ToolSpec] | None,
) -> str:
    payload = args
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
    tool: str, args: ToolArgs, *, frozen_specs: Mapping[str, ToolSpec] | None = None,
) -> tuple[ToolArgs, str]:
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
            return decode_json_object(question_args(args)), ""
        except ValueError as exc:
            return {}, str(exc)
    if name == "done":
        # ``done`` is a control tool with no runtime executor. Its
        # canonical ToolSpec validation above is the complete contract.
        return dict(args), ""
    if name == "edit" and (("browser_source" in args and any(key == "browser_source" for key, _ in spec.parameters))
                           or args.get("declared_fields") is True):
        if args.get("declared_fields") is True and set(args) != {"path", "base_result_ref", "declared_fields"}:
            return {}, "Receipt-bound edit requires only path, base_result_ref and declared_fields=true"
        return dict(args), ""
    if name == "codemode":
        try:
            from codey.codemode.protocol import parse_codemode_args

            program = parse_codemode_args(args if isinstance(args, dict) else {})
            from codey.runtime.core.models import canonical_json_mapping

            normalized: ToolArgs = {
                "code": program.code,
                "language": program.language,
                "inputs": canonical_json_mapping(program.inputs),
            }
            if program.repair_of:
                normalized["repair_of"] = program.repair_of
            return normalized, ""
        except ValueError as exc:
            return {}, str(exc)
    # Custom/third-task tools: generic spec validation is sufficient.
    # They run via injected executors; no legacy coding/research repair.
    if executor not in {"project", "source", "knowledge"}:
        # Controller aliases always lower via research validation.
        is_alias = name in _CONTROLLER_ALIASES
        if is_alias or (name == "source_search" and isinstance(args, dict) and "source_id" in args):
            return _validate_research_args(name, args if isinstance(args, dict) else {})
        return dict(args) if isinstance(args, dict) else {}, ""
    if name in _CONTROLLER_ALIASES or (isinstance(args, dict) and name == "source_search" and "source_id" in args):
        return _validate_research_args(name, args)
    if executor == "project":
        return _validate_project_args(name, args)
    if executor in {"source", "knowledge"}:
        return dict(args), ""
    return {}, "unsupported tool executor"


def _validate_project_args(name: str, args: ToolArgs) -> tuple[ToolArgs, str]:
    from codey.toolchain.execution_options import split_execution_options

    business, _options = split_execution_options(args)
    process_keys = {key: business.pop(key) for key in ("stdin", "tty", "cols", "rows") if key in business}
    if process_keys:
        if name != "run":
            return {}, "process input options require run"
        if args.get("run_in_background") is False:
            return {}, "process input options require managed execution; omit run_in_background or set it true"
        if ("cols" in process_keys or "rows" in process_keys) and process_keys.get("tty") is not True:
            return {}, "terminal dimensions require tty=true"
        if process_keys.get("tty") is True and process_keys.get("stdin") is False:
            return {}, "tty=true conflicts with stdin=false"
    normalized, error = _validate_coding_args(name, business)
    normalized.update(process_keys)
    if "run_in_background" in args:
        normalized["run_in_background"] = args["run_in_background"]
    return normalized, error
