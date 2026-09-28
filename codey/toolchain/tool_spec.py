"""One tool registry for the unified kernel (operations-facing).

Coding definitions, Research contracts, and controller aliases converge here
as ``ToolSpec`` rows: one grant, one parameter schema, one canonical name.
``parallel``/``read_files`` stay hidden until implemented (never advertised
but rejected). Unknown tools are denied, never passed as ``control``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Batch tools stay hidden until the kernel implements them; advertising a tool
# that every call rejects is a contract lie.
_HIDDEN_UNTIL_IMPLEMENTED = frozenset({"parallel", "read_files"})

# Native (runtime) alias -> canonical model name for coding tools.
_NATIVE_TO_CANONICAL = {
    "ls": "list_dir",
    "read": "read_file",
    "search": "grep",
    "references": "find_references",
    "edit": "edit",
    "run": "run",
    "shell": "shell",
    "done": "done",
}

# Controller alias -> canonical research tool + required id arg.
_CONTROLLER_ALIAS_ID_ARG = {
    "open_result": ("open_url", "result_id"),
    "reopen_source": ("open_url", "source_id"),
    "open_hit": ("open_url", "hit_id"),
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    aliases: tuple[str, ...] = ()
    grant: str = ""
    parameters: tuple[tuple[str, object], ...] = ()
    required: tuple[str, ...] = ()
    json_example: str = ""
    description: str = ""
    executor: str = ""
    replay_class: str = "unsafe"


def _coding_specs() -> dict[str, ToolSpec]:
    from codey.toolchain import definition as tool_defs

    specs: dict[str, ToolSpec] = {}
    grant_by_permission = {
        "project_read": "project.read",
        "project_write": "project.write",
        "project_verify": "project.verify",
        "user_approved_shell": "shell.approval",
        "control": "control",
    }
    for definition in tool_defs.TOOL_DEFINITIONS:
        if definition.name in _HIDDEN_UNTIL_IMPLEMENTED:
            continue
        grant = grant_by_permission.get(definition.permission, "")
        if not grant:
            continue
        specs[definition.name] = ToolSpec(
            name=definition.name,
            aliases=tuple(definition.aliases),
            grant=grant,
            parameters=tuple(definition.parameters),
            required=tuple(definition.required),
            json_example=definition.examples[0] if definition.examples else "",
            description=definition.description,
            executor="project",
            replay_class="safe" if definition.read_only else "unsafe",
        )
    return specs


def _research_specs() -> dict[str, ToolSpec]:
    from codey.research.tool_contract import TOOL_CONTRACTS

    grant_by_tool = {
        "web_search": "web.read",
        "open_url": "web.read",
        "source_search": "web.read",
        "knowledge_search": "knowledge.read",
        "knowledge_read": "knowledge.read",
        "knowledge_write": "knowledge.write",
        "knowledge_link": "knowledge.link",
        "done": "control",
    }
    specs: dict[str, ToolSpec] = {}
    for name, grant in grant_by_tool.items():
        contract = TOOL_CONTRACTS.get(name)
        if contract is None:
            continue
        params: list[tuple[str, object]] = []
        for arg_name in contract.required:
            params.append((arg_name, {"type": "string"}))
        for arg_name, arg in contract.optional.items():
            params.append((arg_name, {"type": _research_json_type(arg.type)}))
        specs[name] = ToolSpec(
            name=name,
            aliases=(),
            grant=grant,
            parameters=tuple(params),
            required=tuple(sorted(contract.required.keys())),
            json_example=contract.example,
            description=contract.description,
            executor="source" if grant == "web.read" else "knowledge",
            replay_class="safe" if grant in {"web.read", "knowledge.read"} else "unsafe",
        )
    for alias, (canonical, id_arg) in _CONTROLLER_ALIAS_ID_ARG.items():
        specs[alias] = ToolSpec(
            name=alias,
            aliases=(),
            grant="web.read",
            parameters=((id_arg, {"type": "string"}),),
            required=(id_arg,),
            json_example=f'{{"tool":"{alias}","args":{{"{id_arg}":"..."}}}}',
            description=f"controller alias lowering to {canonical}",
            executor="source",
            replay_class="safe",
        )
    return specs


def _research_json_type(pytype: type) -> str:
    if pytype is int:
        return "integer"
    if pytype is float:
        return "number"
    if pytype is list:
        return "array"
    return "string"


def _all_specs() -> dict[str, ToolSpec]:
    specs = _coding_specs()
    for name, spec in _research_specs().items():
        if name == "done":
            continue
        specs[name] = spec
    if "done" not in specs:
        specs["done"] = _coding_specs().get("done", ToolSpec(name="done", grant="control"))
    return specs


_SPECS: dict[str, ToolSpec] | None = None


def tool_specs() -> dict[str, ToolSpec]:
    global _SPECS
    if _SPECS is None:
        _SPECS = _all_specs()
    return _SPECS


def register_custom_tool(
    name: object,
    *,
    grant: object = "control",
    parameters: tuple[tuple[str, object], ...] = (),
    required: tuple[str, ...] = (),
    description: object = "",
    executor: object = "custom",
) -> bool:
    """Register a third-task tool without editing the kernel loop."""

    canonical = str(name or "").strip().lower()
    if not canonical or canonical in tool_specs():
        return False
    grant_text = str(grant or "").strip().lower()
    try:
        from codey.policies.task_policy import KNOWN_TASK_GRANTS
    except Exception:
        KNOWN_TASK_GRANTS = frozenset({"control"})
    if grant_text not in KNOWN_TASK_GRANTS:
        return False
    tool_specs()[canonical] = ToolSpec(
        name=canonical,
        aliases=(),
        grant=grant_text,
        parameters=tuple(parameters),
        required=tuple(required),
        json_example=f'{{"tool":"{canonical}","args":{{}}}}',
        description=str(description or ""),
        executor=str(executor or "custom"),
        replay_class="unsafe",
    )
    return True


def canonical_tool_name(raw: object) -> str:
    name = str(raw or "").strip().lower()
    if not name:
        return ""
    specs = tool_specs()
    if name in specs:
        return name
    if name in _NATIVE_TO_CANONICAL:
        return _NATIVE_TO_CANONICAL[name]
    for spec in specs.values():
        if name in spec.aliases:
            return spec.name
    return ""


def spec_for_tool(name: object) -> ToolSpec | None:
    canonical = canonical_tool_name(name)
    if not canonical:
        return None
    return tool_specs().get(canonical)


def _policy_allows(policy: Any, grant: str) -> bool:
    allows = getattr(policy, "allows", None)
    if not callable(allows):
        return False
    try:
        return bool(allows(grant))
    except Exception:
        return False


def visible_tool_names(policy: Any) -> tuple[str, ...]:
    return visible_tool_names_for_snapshot(policy, None)


def _is_research_tool(name: str) -> bool:
    try:
        spec = tool_specs().get(str(name or "").strip().lower())
    except Exception:
        return False
    return spec is not None and spec.executor in {"source", "knowledge"}


def visible_tool_names_for_snapshot(policy: Any, controller_allowed: Any = None) -> tuple[str, ...]:
    """Per-turn snapshot: policy ∩ current controller facts.

    The controller may only narrow research tools; it never removes project
    tools from a hybrid run. ``None`` means no controller narrowing.
    """
    allowed: set[str] | None = None
    if controller_allowed is not None:
        try:
            allowed = {str(n or "").strip().lower() for n in controller_allowed}
        except TypeError:
            allowed = None
    order = ("list_dir", "read_file", "grep", "find_references", "edit", "run", "shell",
             "web_search", "open_url", "source_search", "knowledge_search", "knowledge_read",
             "knowledge_write", "knowledge_link", "done")
    names: list[str] = []
    for name in order:
        spec = tool_specs().get(name)
        if spec is None or not spec.grant:
            continue
        if not _policy_allows(policy, spec.grant):
            continue
        if allowed is not None and name != "done" and _is_research_tool(name) and name not in allowed:
            # Controller aliases lower to open_url; keep open_url visible when
            # any alias is allowed so the model can still reach the web.
            if name == "open_url" and bool({"open_url", "open_result", "reopen_source", "open_hit"} & allowed):
                pass
            else:
                continue
        names.append(name)
    # Controller aliases are model-visible names; expose them when allowed.
    if allowed is not None:
        for alias in ("open_result", "reopen_source", "open_hit"):
            spec = tool_specs().get(alias)
            if spec is None or alias in names:
                continue
            if _policy_allows(policy, spec.grant) and alias in allowed:
                names.append(alias)
    # Registered third-task tools advertise alongside built-ins; unregistered
    # names stay denied.
    for name in sorted(tool_specs()):
        if name in names or name in order or name in {"open_result", "reopen_source", "open_hit"}:
            continue
        spec = tool_specs()[name]
        if spec.grant and _policy_allows(policy, spec.grant):
            if allowed is not None and _is_research_tool(name) and name not in allowed:
                continue
            names.append(name)
    return tuple(names)


def _schema_for_spec(spec: ToolSpec) -> dict[str, object]:
    properties: dict[str, object] = {}
    for param_name, schema in spec.parameters:
        properties[str(param_name)] = dict(schema) if isinstance(schema, dict) else {"type": "string"}
    return {
        "type": "object",
        "properties": properties,
        "required": list(spec.required),
        "additionalProperties": False,
    }


def native_tools_for_policy(policy: Any) -> list[dict[str, object]]:
    return native_tools_for_snapshot(policy, None)


def native_tools_for_snapshot(policy: Any, controller_allowed: Any = None) -> list[dict[str, object]]:
    """Native schemas for one turn's snapshot; same source as JSON contract."""
    tools: list[dict[str, object]] = []
    for name in visible_tool_names_for_snapshot(policy, controller_allowed):
        spec = tool_specs().get(name)
        if spec is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec.description or spec.json_example,
                "parameters": _schema_for_spec(spec),
            },
        })
    tools.sort(key=lambda item: str(((item.get("function") or {}).get("name")) or ""))
    return tools


def validate_args_against_spec(name: object, args: dict[str, Any]) -> str:
    """Single authoritative parameter check from the ToolSpec.

    Returns "" when the required ToolSpec params are present; otherwise a
    short error naming the missing arg. Detailed type repair stays in the
    legacy validators, but missing-required is decided here so JSON and
    native prompts, schemas, and validation cannot drift.
    """
    spec = spec_for_tool(name)
    if spec is None:
        return f"unknown tool: {name or '?'}"
    if not isinstance(args, dict):
        return f"{spec.name} args must be an object"
    missing = [key for key in (spec.required or ()) if not str(args.get(key, "") or "").strip()]
    # Allowlist: research validators fill optional defaults, but the kernel
    # only keeps model-supplied args; here only require presence.
    if missing:
        return f"{spec.name} missing required arg '{missing[0]}'"
    return ""


def json_contract_text(policy: Any, *, controller_allowed: Any = None) -> str:
    lines: list[str] = []
    for name in visible_tool_names_for_snapshot(policy, controller_allowed):
        spec = tool_specs().get(name)
        if spec is None:
            continue
        if spec.json_example:
            lines.append(f"- {spec.json_example}  {spec.description}".rstrip())
        else:
            lines.append(f"- {name}")
    return "\n".join(lines)


_CUSTOM_EXECUTORS: dict[str, Any] = {}


def register_custom_executor(name: object, fn: Any) -> bool:
    """Register a production executor for a third-task tool (generic path)."""
    canonical = str(name or "").strip().lower()
    if not canonical or not callable(fn):
        return False
    try:
        spec = spec_for_tool(canonical)
    except Exception:
        spec = None
    if spec is None:
        return False
    _CUSTOM_EXECUTORS[canonical] = fn
    return True


def custom_executor_for(name: object) -> Any | None:
    canonical = str(name or "").strip().lower()
    if not canonical:
        return None
    return _CUSTOM_EXECUTORS.get(canonical)


def unregister_custom_tool(name: object) -> bool:
    canonical = str(name or "").strip().lower()
    if not canonical:
        return False
    removed = False
    try:
        if canonical in tool_specs():
            # Never remove built-ins; only third-task registrations.
            builtin = _all_specs().get(canonical)
            if builtin is None:
                tool_specs().pop(canonical, None)
                removed = True
    except Exception:
        pass
    if canonical in _CUSTOM_EXECUTORS:
        _CUSTOM_EXECUTORS.pop(canonical, None)
        removed = True
    return removed


__all__ = [
    "ToolSpec",
    "canonical_tool_name",
    "custom_executor_for",
    "json_contract_text",
    "native_tools_for_policy",
    "native_tools_for_snapshot",
    "register_custom_executor",
    "register_custom_tool",
    "spec_for_tool",
    "tool_specs",
    "unregister_custom_tool",
    "validate_args_against_spec",
    "visible_tool_names",
    "visible_tool_names_for_snapshot",
]
