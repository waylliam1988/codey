"""Declared tool result data; display text is a separate, derived view."""
from __future__ import annotations

import json
from collections.abc import Mapping

from codey.automation.browser_contracts import InputRelease
from codey.toolchain.constants import PAGE_DETAIL_TOOL
from codey.toolchain.schema_validation import thaw_schema_value
from codey.types.json_value import canonical_json_object, decode_json_object, require_json_integer, require_json_string


def browser_output_schema(name: str) -> dict[str, object]:
    """One structured success contract for direct tools, CodeMode and recovery."""
    string: dict[str, object] = {"type": "string"}
    integer: dict[str, object] = {"type": "integer"}
    number: dict[str, object] = {"type": "number"}
    boolean: dict[str, object] = {"type": "boolean"}

    def record(properties: dict[str, object], required: tuple[str, ...] = ()) -> dict[str, object]:
        return {"type": "object", "additionalProperties": False, "properties": properties, "required": list(required)}

    def array(item: dict[str, object]) -> dict[str, object]:
        return {"type": "array", "items": item}

    document = record({key: string for key in ("target_id", "frame_id", "loader_id", "url")},
                      ("target_id", "frame_id", "loader_id", "url"))
    box = record({key: number for key in ("x", "y", "width", "height")}, ("x", "y", "width", "height"))
    element = record({"ref": string, "role": string, "name": string, "disabled": boolean, "context_id": string,
                      "value": {"anyOf": [string, {"type": "null"}]}, "checked": {"anyOf": [boolean, {"type": "null"}]},
                      "query_relation": string, "record_link": boolean, "box": box},
                     ("ref", "role", "name", "disabled", "context_id"))
    snapshot: dict[str, object] = {
        "page_id": string, "page_url": string, "generation": integer, "revision": integer, "text": string,
        "document": document, "kind": {"type": "string", "enum": ["full", "delta"]}, "base_revision": integer,
        "complete": boolean, "scope": {"anyOf": [string, record({"frame_id": string, "root_ref": string,
            "selector": string, "max_elements": integer, "max_text": integer})]},
        "text_truncated": boolean, "elements_truncated": boolean, "elements": array(element),
        "added": array(element), "changed": array(element), "removed": array(string),
        "scope_suggestions": array(record({"ref": string, "label": string}, ("ref", "label"))),
        "contexts": array(record({**{key: string for key in ("id", "kind", "label", "basis", "text", "quotation", "subject")},
                                  "ordinal": integer})),
        "file_controls": array(record({**{key: string for key in ("id", "name", "dom_id", "field_name", "group")}, "disabled": boolean})),
    }
    action: dict[str, object] = {"action": string, "delivery": string, "text": string}
    observed = {**action, "ref": string, "effect_verified": boolean, "page_url": string, "observation": string,
                "value_length": integer, "values": array(string), "checked": boolean,
                "input_state": record({"keys": array(string), "release": {"type": "string", "enum": [state.value for state in InputRelease]}}, ("keys", "release"))}
    page = {"url": string, "page_id": string, "generation": integer,
            "http_status": {"anyOf": [integer, {"type": "null"}]}, "content_type": string}
    schemas = {
        "browser_snapshot": record(snapshot, ("page_id", "page_url", "generation", "text", "complete", "scope", "elements", "contexts", "text_truncated", "elements_truncated")),
        **{tool: record(observed, ("action", "text")) for tool in
           ("browser_click", "browser_fill", "browser_scroll", "browser_press", "browser_hover", "browser_select", "browser_check")},
        "browser_wait": record({**action, "condition": string, "matched": boolean, "page_url": string, "page_id": string, "document": document}, ("action", "condition", "matched", "text")),
        "browser_navigate": record({**action, **page, "previous_url": string}, ("action", "url", "page_id", "generation", "text")),
        "browser_eval": record({**action, "value": {}, "truncated": boolean}, ("action", "value", "truncated", "text")),
        "browser_tab_list": record({"text": string, "page_url": string, "tabs": array(record({"index": integer, "page_id": string, "url": string, "title": string}))}, ("text", "page_url", "tabs")),
        "browser_tab_new": record({**action, **page, "previous_url": string}, ("action", "url", "page_id", "generation", "text")),
        "browser_tab_switch": record({**action, "index": integer, "url": string, "page_id": string}, ("action", "index", "url", "text")),
        "browser_upload": record({**observed, "attachment_id": string, "name": string, "media_type": string, "bytes": integer, "sha256": string}),
        "browser_download": record({**action, "ref": string, "path": string, "suggested_filename": string, "handle": string, "bytes": integer, "sha256": string}),
        **{tool: record({**action, "frame_id": string, "frames": array(record({"frame_id": string, "url": string,
           "name": string, "document": document, "main": boolean}))}) for tool in ("browser_frame_list", "browser_frame_select")},
        "browser_locate": record({**action, "document": document, "unique": boolean, "candidates": array(record({key: string for key in ("ref", "role", "name", "text", "placeholder")}))}),
        "browser_inspect": record({**action, "ref": string, "tag": string, "attributes": {"type": "object", "additionalProperties": string}, "box": {"anyOf": [box, {"type": "null"}]},
                                   "ancestors": array(record({"tag": string, "role": string, "name": string})), "children": array(record({"tag": string, "text": string}))}),
        "browser_dialog": record({**action, "open": boolean, "type": string, "message": string}),
        "browser_dialog_respond": record(action),
        "browser_visual_click": record({**action, "observation_ref": string, "effect_verified": boolean}),
        "browser_read": text_output_schema(), PAGE_DETAIL_TOOL: text_output_schema(),
    }
    image = record({"observation_ref": string, "document": document,
        "viewport": record({key: number for key in ("width", "height", "x", "y", "scale", "offset_x", "offset_y")}),
        "clip": box, "full_page": boolean, "raw_sha256": string, "sha256": string,
        "width": integer, "height": integer, "media_type": string, "scale_x": number, "scale_y": number,
        "mapping": array(record({"number": integer, "ref": string, "role": string, "name": string, "box": box}))})
    schemas["browser_screenshot"] = record({**snapshot, "image": image})
    return schemas[name]


def text_output_schema() -> dict[str, object]:
    """Text tools explicitly return their model_text, without probing canonical."""
    return {"type": "string"}


def accepted_output_schema() -> dict[str, object]:
    return {"type": "object", "additionalProperties": False, "required": ["kind", "task_ref"],
            "properties": {"kind": {"type": "string", "enum": ["accepted"]},
                           "task_ref": {"type": "string", "minLength": 1}}}


def model_output_schema(completed: Mapping[str, object], *, supports_background: bool) -> dict[str, object]:
    final = canonical_json_object(completed)
    return {"anyOf": [final, accepted_output_schema()]} if supports_background else dict(completed)


def completed_output_schema(model_schema: Mapping[str, object]) -> dict[str, object]:
    """Select the completed branch of the union generated above."""
    schema = canonical_json_object(model_schema)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and len(alternatives) == 2 and alternatives[1] == accepted_output_schema():
        return dict(decode_json_object(alternatives[0]))
    return dict(schema)


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
    from codey.toolchain.schema_validation import _valid_custom_schema, check_schema_value
    from codey.types.json_value import decode_json_value

    decode_json_value(value)
    if not _valid_custom_schema(thaw_schema_value(schema)):
        raise ValueError(f"invalid output schema for {name}")
    error = check_schema_value(name, "result", schema, value, strict=True)
    if error:
        raise ValueError(f"invalid structured result: {error}")


def read_page_line(value: object, line: int) -> str | None:
    """Select an absolute file line; None means this page did not observe it."""
    validate_output("read_file", read_file_output_schema(), value)
    observed = decode_json_object(value)
    end_line = require_json_integer(observed["end_line"], "end_line")
    total_lines = require_json_integer(observed["total_lines"], "total_lines")
    text_complete = observed["text_complete"] is True
    start_line = require_json_integer(observed["start_line"], "start_line")
    if end_line > total_lines:
        raise ValueError("observed page exceeds total lines")
    if not text_complete or not start_line <= line <= end_line:
        return None
    text = require_json_string(observed["text"], "text")
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

        return " | ".join(json.dumps(thaw_schema_value(value), ensure_ascii=False, sort_keys=True) for value in values)
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
