"""Explicit action inputs lowered to the existing frozen CodeMode executor."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from codey.policies.tool_requirement import ToolSuccessRequirement, argument_digest
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, thaw_schema_value, validate_args_with_spec
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, parse_json_object, require_json_string

_LEAVES = {
    "snapshot": "browser_snapshot", "wait": "browser_wait", "read": "browser_read",
    "eval": "browser_eval", "tabs": "browser_tab_list",
}
_CONTROLS = {"click": "browser_click", "fill": "browser_fill", "download": "browser_download"}
_NAVIGATE_TOOL = "browser_navigate"
_EDIT_TOOL = "edit"
_SELECT = """let ref=inputs.target.ref;
if (ref === undefined) {
  const snapshot=unwrap(await tools.browser_snapshot({}));
  if (snapshot.elements_truncated) throw new Error('The control list is truncated; observe a complete list or select an observed ref.');
  const matches=snapshot.elements.filter(e=>e.role===inputs.target.role && e.name===inputs.target.name);
  if (matches.length!==1) throw new Error('Expected one control matching the snapshot role/name. Select an observed ref from candidates: '+JSON.stringify(matches.length ? matches : snapshot.elements));
  ref=matches[0].ref;
}
"""
JSON_EDIT_PROGRAM = """const read=await tools.read_file({path:inputs.path});
const file=unwrap(read);
if (!file.text_complete || file.start_line!==1 || file.next_offset!==0 || file.end_line!==file.total_lines)
  throw new Error('The file exceeds the complete default read supported by edit_json. No edit was attempted; use the general file workflow.');
return unwrap(await tools.edit({read,changes:inputs.changes}));"""


def _object(properties: JsonObject, required: Sequence[str]) -> JsonObject:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _branch(kind: str, properties: JsonObject, required: Sequence[str]) -> JsonObject:
    return _object({"kind": {"type": "string", "enum": [kind]}, **properties}, ("kind", *required))


def action_model_spec(spec: ToolSpec, leaves: Sequence[ToolSpec], navigation_url: str | None) -> ToolSpec:
    """Derive each available action from its authorized frozen leaf definition."""
    by_name = {leaf.name: leaf for leaf in leaves}
    branches: list[JsonValue] = []
    if navigation_url is not None and _NAVIGATE_TOOL in by_name:
        branches.append(_branch("navigate", {"target": {"type": "string", "enum": ["task"]}}, ("target",)))
    for kind, name in _LEAVES.items():
        if name in by_name:
            leaf = by_name[name]
            branches.append(_branch(kind, decode_json_object(thaw_schema_value(dict(leaf.parameters))), leaf.required))
    for kind, name in _CONTROLS.items():
        if name not in by_name:
            continue
        leaf = by_name[name]
        properties = decode_json_object(thaw_schema_value(dict(leaf.parameters)))
        ref = properties.pop("ref")
        targets: list[JsonValue] = [_object({"ref": ref}, ("ref",))]
        if "browser_snapshot" in by_name:
            targets.append(_object({"role": {"type": "string", "minLength": 1, "maxLength": 64},
                "name": {"type": "string", "minLength": 1, "maxLength": 240}}, ("role", "name")))
        properties["target"] = {"anyOf": targets}
        branches.append(_branch(kind, properties, ("target", *(key for key in leaf.required if key != "ref"))))
    if {"read_file", _EDIT_TOOL} <= by_name.keys():
        path = decode_json_object(thaw_schema_value(dict(by_name["read_file"].parameters)))["path"]
        branches.append(_branch("edit_json", {"path": path, "changes": {"type": "object"}}, ("path", "changes")))
    if not branches:
        raise ValueError("Structured actions have no supported authorized leaves.")
    return freeze_spec_parameters(replace(spec, parameters=(("action", {"anyOf": branches}),), required=("action",),
        declarations="", json_examples=(), description=
        "Submit one structured action from this turn's schema; do not submit JavaScript. "
        "Navigate uses the frozen task URL without copying it. Named controls match the role and name in a fresh "
        "main-frame visible-control snapshot (names have whitespace folded and are limited to 240 characters). "
        "When names repeat, choose an observed ref; stale refs remain rejected. "
        "edit_json replaces existing top-level fields with explicit values, using a complete default file read and "
        "the original receipt. It does not support files outside that read window or integers outside JavaScript's "
        "safe range. A version conflict requires another action with a fresh read; uncertain delivery requires observation."))


def lower_action(args: JsonObject) -> JsonObject:
    if set(args) != {"action"}:
        raise ValueError("Submit one action without JavaScript arguments.")
    action = dict(decode_json_object(args["action"]))
    kind = require_json_string(action.pop("kind"), "kind")
    if kind == "navigate":
        code = f'return unwrap(await tools.{_NAVIGATE_TOOL}({{target:"task"}}));'
    elif kind in _LEAVES:
        code = f"return unwrap(await tools.{_LEAVES[kind]}(inputs));"
    elif kind in _CONTROLS:
        call = "{ref,value:inputs.value}" if kind == "fill" else "{ref}"
        code = _SELECT + f"return unwrap(await tools.{_CONTROLS[kind]}({call}));"
    elif kind == "edit_json":
        if not decode_json_object(action["changes"]):
            raise ValueError("changes must name at least one existing top-level JSON field")
        code = JSON_EDIT_PROGRAM
    else:
        raise ValueError("Unknown structured action.")
    return {"code": code, "inputs": action}


@dataclass(frozen=True)
class RequiredAction:
    kind: str
    action: JsonObject
    arguments: JsonObject
    missing: tuple[str, ...] = ()
    error: str = ""


def required_action(requirement: ToolSuccessRequirement, spec: ToolSpec,
                    navigation_url: str | None, leaves: Sequence[ToolSpec]) -> RequiredAction:
    """Project a leaf selector through the same fixed lowering used at execution."""
    from codey.codemode.protocol import parse_codemode_args

    kinds = {name: kind for kind, name in {**_LEAVES, **_CONTROLS,
                                         "navigate": _NAVIGATE_TOOL, "edit_json": _EDIT_TOOL}.items()}
    kind = kinds.get(requirement.tool, "")
    args = parse_json_object(requirement.arguments_json)
    action: JsonObject = {"kind": kind, **args}
    if requirement.via not in {"", "codemode"} or not kind:
        return RequiredAction(kind, action, args, error="No corresponding action can establish the required execution.")
    properties = decode_json_object(thaw_schema_value(dict(spec.parameters)))
    branches = decode_json_object(properties["action"])["anyOf"]
    assert isinstance(branches, list)
    branch = next((decode_json_object(row) for row in branches
        if decode_json_object(decode_json_object(row)["properties"])["kind"] == {"type": "string", "enum": [kind]}), None)
    if branch is None:
        return RequiredAction(kind, action, args, error="This action is not published by the frozen interface.")
    leaf = next((item for item in leaves if item.name == requirement.tool), None)
    if leaf is None:
        return RequiredAction(kind, action, args, error="The frozen interface has no corresponding leaf definition.")
    # Selectors are constraints on actual committed child arguments, not on the
    # model action wrapper. Validate known fields before transforming them.
    if error := validate_args_with_spec(replace(leaf, required=()), args):
        return RequiredAction(kind, action, args, error=error)
    if kind == "navigate":
        if any(key != "url" or value != navigation_url for key, value in args.items()):
            return RequiredAction(kind, action, args, error="The required URL does not match the frozen task URL.")
        action = {"kind": kind, "target": "task"}
        args = {"url": navigation_url}
    elif kind in _CONTROLS:
        action = {"kind": kind, **{key: value for key, value in args.items() if key != "ref"}}
        if "ref" in args:
            action["target"] = {"ref": args["ref"]}
    elif kind == "edit_json" and set(args) - {"path"}:
        return RequiredAction(kind, action, args,
            error="edit_json cannot guarantee these selectors on the lowered edit arguments; explicit field changes are required.")
    required = branch["required"]
    assert isinstance(required, list)
    missing = tuple(key for key in required if isinstance(key, str) and key not in action)
    if missing:
        return RequiredAction(kind, action, args, missing)
    error = validate_args_with_spec(spec, {"action": action}) or ""
    if not error:
        try:
            parse_codemode_args(lower_action({"action": action}))
        except (KeyError, ValueError, TypeError) as exc:
            error = str(exc)
    return RequiredAction(kind, action, args, error=error)


def action_covers(candidate: RequiredAction, requirement: ToolSuccessRequirement) -> bool:
    """Coalesce suggested calls only; every completion rule still checks its receipts."""
    expected = parse_json_object(requirement.arguments_json)
    return (not candidate.error and not candidate.missing and requirement.via in {"", "codemode"}
        and all(key in candidate.arguments and argument_digest(candidate.arguments[key]) == argument_digest(value)
                for key, value in expected.items()))


def action_failure(result: ToolResult, spec: ToolSpec, children: Mapping[str, ToolResult]) -> ToolResult:
    from codey.operations.browser_copy import write_unconfirmed
    from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
    from codey.toolchain.tool_prompt import tool_repair_text

    conflict = any(child.call.name == "edit" and child.audit.get("error_code") == STALE_READ_REFERENCE_CODE
                   for child in children.values())
    detail = "The structured action did not complete.\n" + ("File version conflict before writing." if conflict else result.model_text)
    audit = {**result.audit, "simplified_input": True}
    if conflict:
        audit["runtime_error"] = result.model_text
    if result.audit.get("error_code") in {"syntax_error", "missing_return", "result_encoding", "result_contract"}:
        detail = "The fixed action template failed; the model cannot repair its JavaScript.\n" + detail
        audit["template_error"] = result.model_text
    for ref, child in children.items():
        if write_unconfirmed(child):
            detail += "\nThe file write occurred but workspace identity is unconfirmed. Do not repeat the write."
        elif child.call.name == "edit" and child.ok:
            detail += "\nThe file write completed. Do not repeat the write."
        elif child.call.name == "edit" and child.audit.get("error_code") == STALE_READ_REFERENCE_CODE:
            detail += ("\nThe version conflict was rejected before writing. Resubmit the edit_json action with the "
                       "intended field changes; the fixed program obtains a fresh complete read and receipt.")
        value = child.canonical.get("value")
        if not isinstance(value, dict) or "delivery" not in value:
            continue
        value = decode_json_object(value)
        leaf: JsonObject = {"tool": child.call.name, "ok": child.ok, "value": value}
        detail += "\nLeaf receipt " + ref + ": " + json.dumps(leaf, ensure_ascii=False)
        delivery = value["delivery"]
        if isinstance(delivery, str) and delivery in {"delivered", "uncertain", "verified", "unverified"} and not child.ok:
            detail += "\nThe action may already have taken effect. Observe the page before deciding another action; do not blindly repeat it."
        elif value.get("reason") == "stale_ref":
            detail += "\nThis action was not delivered. Take a fresh snapshot and explicitly select a current target."
    if children:
        detail += "\nSettled child receipts (effects are not undone): " + ", ".join(children)
    return replace(result, audit=audit, model_text=tool_repair_text(detail, spec))
