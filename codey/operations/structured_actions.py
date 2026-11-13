"""Explicit action inputs lowered to the existing frozen CodeMode executor."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace

from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, thaw_schema_value
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string

_LEAVES = {
    "snapshot": "browser_snapshot", "wait": "browser_wait", "read": "browser_read",
    "eval": "browser_eval", "tabs": "browser_tab_list",
}
_CONTROLS = {"click": "browser_click", "fill": "browser_fill", "download": "browser_download"}
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
    if navigation_url is not None and "browser_navigate" in by_name:
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
    if {"read_file", "edit"} <= by_name.keys():
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
        code = 'return unwrap(await tools.browser_navigate({target:"task"}));'
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


def action_failure(result: ToolResult, spec: ToolSpec, children: Mapping[str, ToolResult]) -> ToolResult:
    from codey.operations.browser_copy import write_unconfirmed
    from codey.toolchain.constants import STALE_READ_REFERENCE_CODE
    from codey.toolchain.tool_prompt import tool_repair_text

    detail = "The structured action did not complete.\n" + result.model_text
    audit = {**result.audit, "simplified_input": True}
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
