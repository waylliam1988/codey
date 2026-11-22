"""Compose browser observations under the existing CodeMode catalog."""
from __future__ import annotations

from collections.abc import Mapping

from codey.codemode.types import ToolCatalogEntry
from codey.toolchain.tool_spec import _check_spec_value_against_schema
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object, decode_json_object

_FLOW_TOOLS = ("browser_navigate", "browser_snapshot", "browser_wait", "browser_read", "browser_tab_list")


def flow_schema(entries: Mapping[str, ToolCatalogEntry]) -> JsonObject:
    steps: list[JsonValue] = []
    for name in _FLOW_TOOLS:
        if name not in entries:
            continue
        args = canonical_json_object(entries[name].parameters)
        steps.append({"type": "object", "properties": {"tool": {"type": "string", "enum": [name]}, "args": args},
                      "required": ["tool", "args"], "additionalProperties": False})
    return {"type": "object", "properties": {"flow": {"type": "array", "items": {"anyOf": steps},
            "minItems": 1, "maxItems": 6}}, "required": ["flow"], "additionalProperties": False}


def flow_steps(args: JsonObject, contract: JsonObject) -> tuple[tuple[str, JsonObject], ...]:
    if error := _check_spec_value_against_schema("browser_snapshot", "args", contract, args, strict=True):
        raise ValueError(error)
    raw = args.get("flow")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 6:
        raise ValueError("browser flow requires one to six observation steps")
    steps: list[tuple[str, JsonObject]] = []
    for item in raw:
        row = decode_json_object(item)
        name = row.get("tool")
        if not isinstance(name, str) or name not in _FLOW_TOOLS:
            raise ValueError("the flow contains an action outside its observation contract")
        steps.append((name, decode_json_object(row.get("args"))))
    return tuple(steps)
