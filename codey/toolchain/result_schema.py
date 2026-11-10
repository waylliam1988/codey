"""Declared tool result data; display text is a separate, derived view."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast


def browser_output_schema(name: str) -> dict[str, object]:
    """Browser success values, including the delegate's text projection."""
    string: dict[str, object] = {"type": "string"}
    integer: dict[str, object] = {"type": "integer"}
    boolean: dict[str, object] = {"type": "boolean"}

    def record(properties: dict[str, object]) -> dict[str, object]:
        return {"type": "object", "additionalProperties": False, "properties": properties,
                "required": list(properties)}

    action: dict[str, object] = {"action": string, "delivery": string, "text": string}
    page = {"url": string, "page_id": string, "generation": integer,
            "http_status": {"anyOf": [integer, {"type": "null"}]}, "content_type": string}
    observed = {**action, "ref": string, "observation": string, "effect_verified": boolean, "page_url": string}
    schemas = {
        "browser_snapshot": record({
            "page_id": string, "page_url": string, "generation": integer, "text": string,
            "complete": boolean, "scope": string, "text_truncated": boolean, "elements_truncated": boolean,
            "elements": {"type": "array", "items": record({
                "ref": string, "role": string, "name": string, "disabled": boolean,
                "value": {"anyOf": [string, {"type": "null"}]},
                "checked": {"anyOf": [boolean, {"type": "null"}]},
            })},
        }),
        "browser_click": record(observed),
        "browser_scroll": record(observed),
        "browser_fill": record({**action, "ref": string, "value_length": integer}),
        "browser_wait": record({"action": string, "condition": string, "matched": boolean,
                                "page_url": string, "text": string}),
        "browser_navigate": record({**action, **page, "previous_url": string}),
        "browser_eval": record({**action, "value": {}, "truncated": boolean}),
        "browser_tab_list": record({"text": string, "page_url": string, "tabs": {"type": "array", "items": record({
            "index": integer, "page_id": string, "url": string, "title": string})}}),
        "browser_tab_new": record({**action, **page}),
        "browser_tab_switch": record({**action, "index": integer, "url": string}),
        "browser_upload": record({**action, "ref": string, "attachment_id": string, "name": string,
                                  "media_type": string, "bytes": integer, "sha256": string, "effect_verified": boolean}),
        "browser_download": record({**action, "ref": string, "path": string, "suggested_filename": string,
                                    "handle": string, "bytes": integer, "sha256": string}),
        "browser_read": text_output_schema(),
    }
    return schemas[name]


def text_output_schema() -> dict[str, object]:
    """Text tools explicitly return their model_text, without probing canonical."""
    return {"type": "string"}


def read_file_output_schema() -> dict[str, object]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["path", "text", "start_line", "end_line", "total_lines", "next_offset", "text_complete"],
        "properties": {
            "path": {"type": "string"}, "text": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 0},
            "total_lines": {"type": "integer", "minimum": 0},
            "next_offset": {"type": "integer", "minimum": 0},
            "text_complete": {"type": "boolean"},
        },
    }


def search_output_schema() -> dict[str, object]:
    """Canonical search pages, shared by declarations, receipts and CodeMode."""
    properties: dict[str, object] = {
        **{key: {"type": "string"} for key in ("query", "path", "include")},
        "exclude": {"type": "array", "items": {"type": "string"}},
        **{key: {"type": "boolean"} for key in ("literal", "case_sensitive", "has_more", "scan_complete")},
        **{key: {"type": "integer", "minimum": 1} for key in ("offset", "limit")},
        **{key: {"type": "integer", "minimum": 0} for key in ("before_context", "after_context")},
        "next_offset": {"type": "integer", "minimum": 0},
        "matches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "line", "column_byte", "text"],
            "properties": {"path": {"type": "string"}, "text": {"type": "string"},
                           "line": {"type": "integer", "minimum": 1},
                           "column_byte": {"type": "integer", "minimum": 1}}}},
        "context": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "line", "text", "relation"],
            "properties": {"path": {"type": "string"}, "text": {"type": "string"},
                           "line": {"type": "integer", "minimum": 1},
                           "relation": {"type": "string", "enum": ["before", "after", "between"]}}}},
        "coverage": {"type": "object", "additionalProperties": False,
                     "required": ["files_admitted", "bytes_admitted", "oversized", "unreadable", "budget_limited"],
                     "properties": {**{key: {"type": "integer", "minimum": 0}
                                       for key in ("files_admitted", "bytes_admitted", "oversized", "unreadable")},
                                    "budget_limited": {"type": "boolean"}}},
    }
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": list(properties)}


def validate_output(name: str, schema: Mapping[str, object], value: object) -> None:
    """Use the existing tool schema validator for successful result values."""
    from codey.toolchain.tool_spec import _check_spec_value_against_schema, _valid_custom_schema, thaw_schema_value
    from codey.types.json_value import decode_json_value

    decode_json_value(value)
    if not _valid_custom_schema(thaw_schema_value(schema)):
        raise ValueError(f"invalid output schema for {name}")
    error = _check_spec_value_against_schema(name, "result", schema, value, strict=True)
    if error:
        raise ValueError(f"invalid structured result: {error}")


def read_page_line(value: object, line: int) -> str | None:
    """Select an absolute file line; None means this page did not observe it."""
    validate_output("read_file", read_file_output_schema(), value)
    observed = cast(Mapping[str, object], value)
    end_line = cast(int, observed["end_line"])
    total_lines = cast(int, observed["total_lines"])
    text_complete = cast(bool, observed["text_complete"])
    start_line = cast(int, observed["start_line"])
    if end_line > total_lines:
        raise ValueError("observed page exceeds total lines")
    if not text_complete or not start_line <= line <= end_line:
        return None
    text = cast(str, observed["text"])
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    if len(lines) != end_line - start_line + 1:
        raise ValueError("observed page coordinates differ from text")
    return lines[line - start_line]


def schema_typescript(schema: Mapping[str, object]) -> str:
    """Render types and constraints from the same frozen schema the host validates."""
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, (list, tuple)):
        return " | ".join(schema_typescript(item) for item in alternatives if isinstance(item, Mapping))
    values = schema.get("enum")
    if isinstance(values, (list, tuple)):
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in values)
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        if not isinstance(properties, Mapping):
            return "{ [key: string]: unknown }"
        required_keys = required if isinstance(required, (list, tuple)) else ()
        fields = [
            f"{json.dumps(key)}{'' if key in required_keys else '?'}: "
            f"{schema_typescript(value) if isinstance(value, Mapping) else 'unknown'}"
            for key, value in properties.items()
        ]
        additional = schema.get("additionalProperties")
        if isinstance(additional, Mapping):
            fields.append(f"[key: string]: {schema_typescript(additional)}")
        return "{ " + "; ".join(fields) + " }"
    if kind == "array":
        items = schema.get("items")
        result = f"Array<{schema_typescript(items) if isinstance(items, Mapping) else 'unknown'}>"
    else:
        result = {"string": "string", "integer": "number", "number": "number",
                  "boolean": "boolean", "null": "null"}.get(str(kind), "unknown")
    constraints = []
    for label, low, high in (("length", "minLength", "maxLength"), ("items", "minItems", "maxItems"),
                             ("range", "minimum", "maximum")):
        if low in schema or high in schema:
            constraints.append(f"{label} {schema.get(low, '*')}..{schema.get(high, '*')}")
    if "pattern" in schema:
        constraints.append("pattern " + json.dumps(schema["pattern"]))
    if schema.get("uniqueItems"):
        constraints.append("distinct items")
    return result + (" /* " + "; ".join(constraints) + " */" if constraints else "")
