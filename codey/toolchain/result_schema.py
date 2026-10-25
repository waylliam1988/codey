"""Declared tool result data; display text is a separate, derived view."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


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
        **{key: {"type": "boolean"} for key in ("literal", "case_sensitive", "has_more", "scan_complete")},
        **{key: {"type": "integer", "minimum": 1} for key in ("offset", "limit")},
        "next_offset": {"type": "integer", "minimum": 0},
        "matches": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["path", "line", "column_byte", "text"],
            "properties": {"path": {"type": "string"}, "text": {"type": "string"},
                           "line": {"type": "integer", "minimum": 1},
                           "column_byte": {"type": "integer", "minimum": 1}}}},
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

    if not _valid_custom_schema(thaw_schema_value(schema)):
        raise ValueError(f"invalid output schema for {name}")
    error = _check_spec_value_against_schema(name, "result", schema, value, strict=True)
    if error:
        raise ValueError(f"invalid structured result: {error}")


def read_page_line(value: Any, line: int) -> str | None:
    """Select an absolute file line; None means this page did not observe it."""
    validate_output("read_file", read_file_output_schema(), value)
    if value["end_line"] > value["total_lines"]:
        raise ValueError("observed page exceeds total lines")
    if not value["text_complete"] or not value["start_line"] <= line <= value["end_line"]:
        return None
    lines = value["text"].split("\n")
    if value["text"].endswith("\n"):
        lines.pop()
    if len(lines) != value["end_line"] - value["start_line"] + 1:
        raise ValueError("observed page coordinates differ from text")
    return str(lines[line - value["start_line"]])


def schema_typescript(schema: Mapping[str, Any]) -> str:
    """Render declarations from the same schema validated by the host."""
    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        fields = [f"{json.dumps(key)}{'' if key in required else '?'}: {schema_typescript(value)}"
                  for key, value in properties.items()]
        return "{ " + "; ".join(fields) + " }"
    if kind == "array":
        return f"Array<{schema_typescript(schema['items']) if 'items' in schema else 'unknown'}>"
    return {"string": "string", "integer": "number", "number": "number",
            "boolean": "boolean"}.get(str(kind), "unknown")
