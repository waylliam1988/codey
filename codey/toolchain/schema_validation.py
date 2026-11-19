"""Bounded schema vocabulary accepted by custom tool registration."""
from __future__ import annotations

from collections.abc import Mapping


def _allowed_schema_keys(kind: str) -> set[str]:
    if kind in {"integer", "number"}:
        return {"type", "minimum", "maximum", "enum"}
    if kind == "string":
        return {"type", "enum", "minLength", "maxLength", "pattern"}
    if kind == "array":
        return {"type", "items", "enum", "minItems", "maxItems", "uniqueItems"}
    if kind == "object":
        return {"type", "properties", "required", "additionalProperties", "enum"}
    if kind == "boolean":
        return {"type", "enum"}
    return {"type"}


def _valid_schema_bounds(kind: str, schema: Mapping[str, object]) -> bool:
    for bound_key in ("minimum", "maximum"):
        if bound_key not in schema:
            continue
        if kind not in {"integer", "number"}:
            return False
        bound = schema[bound_key]
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            return False
        try:
            import math as _math
            if isinstance(bound, float) and not _math.isfinite(bound):
                return False
        except Exception:
            return False
    if "minimum" in schema and "maximum" in schema:
        try:
            minimum = schema["minimum"]
            maximum = schema["maximum"]
            if not isinstance(minimum, (int, float)) or not isinstance(maximum, (int, float)):
                return False
            if float(minimum) > float(maximum):
                return False
        except Exception:
            return False
    return True


def _valid_schema_nested(kind: str, schema: Mapping[str, object]) -> bool:
    if kind == "array" and "items" in schema:
        return _valid_custom_schema(schema["items"])
    if kind != "object":
        return True
    props = schema.get("properties", {})
    if "properties" in schema:
        if not isinstance(props, dict):
            return False
        for _ps in props.values():
            if not _valid_custom_schema(_ps):
                return False
    if "required" in schema and not isinstance(schema["required"], list):
        return False
    extra = schema.get("additionalProperties", True)
    return isinstance(extra, bool) or _valid_custom_schema(extra)


def _valid_custom_schema(schema: object) -> bool:
    if not isinstance(schema, Mapping):
        return False
    if not schema:
        return True
    if "anyOf" in schema:
        alternatives = schema["anyOf"]
        return (schema.keys() == {"anyOf"} and isinstance(alternatives, (list, tuple)) and bool(alternatives)
                and all(_valid_custom_schema(item) for item in alternatives))
    kind = schema.get("type")
    if kind not in {"string", "integer", "number", "boolean", "object", "array", "null"}:
        return False
    if any(key not in _allowed_schema_keys(str(kind)) for key in schema):
        return False
    if not _valid_schema_bounds(str(kind), schema):
        return False
    if "enum" in schema and not isinstance(schema["enum"], list):
        return False
    return _valid_schema_nested(str(kind), schema)
