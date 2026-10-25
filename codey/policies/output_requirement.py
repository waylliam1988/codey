"""Explicit final-output checks in the original immutable required_checks."""
from __future__ import annotations

import hashlib
import json
from typing import Any

PREFIX = "output_json"
JSON_ANSWER_GUIDANCE = (
    "Final answer must be JSON: one bare document without Markdown fences or surrounding prose."
)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def declaration(format: str, schema_json: str = "") -> str:
    if format not in {"text", "json"}:
        raise ValueError("output_format must be text or json")
    if schema_json and format != "json":
        raise ValueError("output schema requires JSON format")
    if not schema_json:
        return PREFIX if format == "json" else ""
    from codey.toolchain.tool_spec import _valid_custom_schema

    schema = json.loads(schema_json, object_pairs_hook=_unique, parse_constant=_constant)
    if not _valid_custom_schema(schema):
        raise ValueError("unsupported output JSON schema")
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > 16000:
        raise ValueError("output schema exceeds budget")
    return PREFIX + ":" + encoded


def is_output_check(check: str) -> bool:
    return check == PREFIX or check.startswith(PREFIX + ":")


def check_id(check: str) -> str:
    return PREFIX if check == PREFIX else PREFIX + ":" + hashlib.sha256(check.encode()).hexdigest()[:32]


def evaluate(check: str, text: str) -> tuple[bool, str, str]:
    schema = check[len(PREFIX) + 1:] if check.startswith(PREFIX + ":") else ""
    if declaration("json", schema) != check:
        raise ValueError("noncanonical output requirement")
    try:
        value = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if schema:
            from codey.toolchain.result_schema import validate_output

            validate_output("final_answer", json.loads(schema), value)
    except (ValueError, TypeError, RecursionError) as exc:
        return False, "output_format_invalid", (
            "Repair only the final answer. " + JSON_ANSWER_GUIDANCE + " "
            "Reuse committed tool results; do not repeat tools or tests for an answer-format repair. "
            f"Validation: {str(exc)[:240]}" + (f"\nRequired schema: {schema}" if schema else "")
        )
    return True, "", ""


def guidance(checks: tuple[str, ...]) -> str:
    rows = [check for check in checks if is_output_check(check)]
    if not rows:
        return ""
    return JSON_ANSWER_GUIDANCE + "".join(
        "\nRequired JSON schema: " + row[len(PREFIX) + 1:] for row in rows if row != PREFIX)


def submission_fields(body: dict[str, Any]) -> dict[str, str]:
    format, schema = body.get("output_format", "text"), body.get("output_schema", "")
    if not isinstance(format, str) or not isinstance(schema, str):
        raise ValueError("output_format and output_schema must be strings")
    declaration(format, schema)
    return {"output_format": format, "output_schema": schema} if format != "text" or schema else {}
