"""Explicit final-output checks in the original immutable required_checks."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import NoReturn

from codey.types.json_value import JsonObject, JsonValue, decode_json_object, decode_json_value, parse_json_object

PREFIX = "output_json"
EXPLANATION_PREFIX = "output_json_explanation"
EXPLANATION_GUIDANCE = (
    "The application assembles the final JSON. In done.summary, supply only the natural-language explanation, "
    "not an inner JSON document. The application preserves that exact string in /explanation and fills declared "
    "receipt-bound fields from committed tool results. Repeat selected values in the explanation only when explicitly required. "
    "The required JSON schema describes the application's final output."
)
JSON_ANSWER_GUIDANCE = (
    "Final answer must be JSON: one bare document without Markdown fences or surrounding prose."
)


def _unique(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _constant(value: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant: {value}")


def declaration(format: str, schema_json: str = "", *, strict_research: bool = False) -> str:
    if format not in {"text", "json", "json_explanation"}:
        raise ValueError("output_format must be text, json or json_explanation")
    if schema_json and format == "text":
        raise ValueError("output schema requires JSON format")
    if strict_research and format == "json_explanation":
        raise ValueError("json_explanation cannot replace the strict Research answer contract")
    prefix = EXPLANATION_PREFIX if format == "json_explanation" else PREFIX
    if not schema_json:
        if format != "json_explanation":
            return PREFIX if format == "json" else ""
        schema_json = '{"type":"object","properties":{"explanation":{"type":"string"}},"required":["explanation"]}'
    from codey.toolchain.tool_spec import _valid_custom_schema

    loaded: object = json.loads(schema_json, object_pairs_hook=_unique, parse_constant=_constant)
    schema = decode_json_value(loaded)
    if not _valid_custom_schema(schema):
        raise ValueError("unsupported output JSON schema")
    if format == "json_explanation":
        root = decode_json_object(schema)
        properties = root.get("properties")
        explanation = properties.get("explanation") if isinstance(properties, dict) else None
        required = root.get("required")
        if (root.get("type") != "object" or not isinstance(explanation, dict)
                or explanation.get("type") != "string" or not isinstance(required, list)
                or "explanation" not in required):
            raise ValueError("json_explanation requires an object schema with a required string explanation")
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > 16000:
        raise ValueError("output schema exceeds budget")
    return prefix + ":" + encoded


def is_output_check(check: str) -> bool:
    return check == PREFIX or check.startswith((PREFIX + ":", EXPLANATION_PREFIX + ":"))


def has_explanation(checks: tuple[str, ...]) -> bool:
    return any(check.startswith(EXPLANATION_PREFIX + ":") for check in checks)


def schema_for(check: str) -> JsonObject | None:
    return parse_json_object(check.partition(":")[2]) if ":" in check else None


def check_id(check: str) -> str:
    return PREFIX if check == PREFIX else PREFIX + ":" + hashlib.sha256(check.encode()).hexdigest()[:32]


def evaluate(check: str, text: str) -> tuple[bool, str, str]:
    schema = check.partition(":")[2]
    explanation = has_explanation((check,))
    if declaration("json_explanation" if explanation else "json", schema) != check:
        raise ValueError("noncanonical output requirement")
    value: JsonValue = None
    try:
        loaded: object = json.loads(text, object_pairs_hook=_unique, parse_constant=_constant)
        value = decode_json_value(loaded)
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if schema:
            from codey.toolchain.result_schema import validate_output

            required_schema = parse_json_object(schema)
            validate_output("final_answer", required_schema, value)
    except (ValueError, TypeError, RecursionError) as exc:
        draft = value.get("explanation") if explanation and isinstance(value, dict) else None
        return False, "output_format_invalid", (
            "Repair only the final answer. " + (EXPLANATION_GUIDANCE if explanation else JSON_ANSWER_GUIDANCE) + " "
            "Reuse committed tool results; do not repeat tools or tests for an answer-format repair. "
            f"Validation: {str(exc)[:240]}" + (f"\nRequired schema: {schema}" if schema else "")
            + ("\n\nRejected explanation to revise:\n" + draft if isinstance(draft, str) else "")
        )
    return True, "", ""


def guidance(checks: tuple[str, ...]) -> str:
    rows = [check for check in checks if is_output_check(check)]
    if not rows:
        return ""
    return (EXPLANATION_GUIDANCE if has_explanation(checks) else JSON_ANSWER_GUIDANCE) + "".join(
        "\nRequired JSON schema: " + row.partition(":")[2] for row in rows if row != PREFIX)


def submission_fields(body: Mapping[str, object]) -> dict[str, str]:
    format, schema = body.get("output_format", "text"), body.get("output_schema", "")
    if not isinstance(format, str) or not isinstance(schema, str):
        raise ValueError("output_format and output_schema must be strings")
    declaration(format, schema)
    return {"output_format": format, "output_schema": schema} if format != "text" or schema else {}
