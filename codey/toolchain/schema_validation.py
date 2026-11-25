"""Bounded schema vocabulary accepted by custom tool registration."""
from __future__ import annotations

from collections.abc import Mapping, Sequence


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


def _spec_type_error(spec_name: str, key: str, want: str, got: object) -> str:
    try:
        got_name = type(got).__name__
    except Exception:
        got_name = "unknown"
    return f"{spec_name} arg '{key}' must be {want} (got {got_name})"


def _spec_want_and_bounds(schema: object) -> tuple[str, int | float | None, int | float | None]:
    want: str = ""
    minimum: int | float | None = None
    maximum: int | float | None = None
    try:
        if isinstance(schema, Mapping):
            want = str(schema.get("type", "") or "").strip().lower()
            if "minimum" in schema:
                raw_min = schema.get("minimum")
                if isinstance(raw_min, bool):
                    minimum = None
                elif isinstance(raw_min, (int, float)):
                    minimum = raw_min
            if "maximum" in schema:
                raw_max = schema.get("maximum")
                if isinstance(raw_max, bool):
                    maximum = None
                elif isinstance(raw_max, (int, float)):
                    maximum = raw_max
    except Exception:
        want, minimum, maximum = "", None, None
    return want, minimum, maximum


def _check_string_value(spec_name: str, key: str, value: object) -> str:
    if not isinstance(value, str):
        return _spec_type_error(spec_name, key, "string", value)
    return ""


def _check_integer_value(
    spec_name: str, key: str, value: object, minimum: int | float | None,
    maximum: int | float | None = None, *, strict: bool = False,
) -> str:
    if isinstance(value, bool):
        return _spec_type_error(spec_name, key, "integer", value)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        try:
            import math as _math

            if not _math.isfinite(value) or not float(value).is_integer():
                return _spec_type_error(spec_name, key, "integer", value)
            parsed = int(value)
        except Exception:
            return _spec_type_error(spec_name, key, "integer", value)
    elif isinstance(value, str):
        if strict:
            return _spec_type_error(spec_name, key, "integer", value)
        text = value.strip()
        if not text or not text.isascii():
            return _spec_type_error(spec_name, key, "integer", value)
        try:
            parsed = int(text)
        except ValueError:
            return _spec_type_error(spec_name, key, "integer", value)
    else:
        return _spec_type_error(spec_name, key, "integer", value)
    if minimum is not None:
        try:
            if parsed < minimum:
                return f"{spec_name} arg '{key}' must be >= {minimum}"
        except Exception:
            pass
    if maximum is not None:
        try:
            if parsed > maximum:
                return f"{spec_name} arg '{key}' must be <= {maximum}"
        except Exception:
            pass
    return ""


def _check_number_value(
    spec_name: str, key: str, value: object, schema: object = None, *, strict: bool = False,
) -> str:
    import math

    _, minimum, maximum = _spec_want_and_bounds(schema)
    if isinstance(value, bool):
        return _spec_type_error(spec_name, key, "number", value)
    number = value
    if isinstance(value, str) and not strict:
        try:
            number = float(value.strip())
        except ValueError:
            return _spec_type_error(spec_name, key, "number", value)
    if not isinstance(number, (int, float)) or (isinstance(number, float) and not math.isfinite(number)):
        return _spec_type_error(spec_name, key, "number", value)
    if minimum is not None and number < minimum:
        return f"{spec_name} arg '{key}' must be >= {minimum}"
    if maximum is not None and number > maximum:
        return f"{spec_name} arg '{key}' must be <= {maximum}"
    return ""


def _check_boolean_value(spec_name: str, key: str, value: object) -> str:
    if not isinstance(value, bool):
        return _spec_type_error(spec_name, key, "boolean", value)
    return ""


def _check_array_value(spec_name: str, key: str, schema: object, value: object, *, strict: bool = False) -> str:
    if not isinstance(value, list):
        return _spec_type_error(spec_name, key, "array", value)
    items: list[object] = value
    if not isinstance(schema, Mapping) or schema.get("items") is None:
        return ""
    for bound_name, compare in (("minItems", len(value) < int(schema.get("minItems", 0))),
                           ("maxItems", len(value) > int(schema.get("maxItems", len(value))))):
        if compare:
            return f"{spec_name} arg '{key}' violates {bound_name}={schema[bound_name]}"
    if schema.get("uniqueItems") and any(items[i] == items[j] for i in range(len(items)) for j in range(i)):
        return f"{spec_name} arg '{key}' requires unique items"
    for index, item in enumerate(items):
        error = check_schema_value(spec_name, f"{key}[{index}]", schema["items"], item, strict=strict)
        if error:
            return error
    return ""


def _check_object_value(spec_name: str, key: str, schema: object, value: object, *, strict: bool = False) -> str:
    if not isinstance(value, dict):
        return _spec_type_error(spec_name, key, "object", value)
    if isinstance(schema, Mapping):
        required = schema.get("required", ())
        if isinstance(required, (list, tuple)):
            for required_key in required:
                # 通用层严格按声明检查，不接受 edit 的历史别名。
                if required_key not in value:
                    return f"{spec_name} arg '{key}' missing required property '{required_key}'"
        properties: object = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for child_key, child_value in value.items():
                canonical_child_key = str(child_key)
                child_schema = properties.get(canonical_child_key)
                if child_schema is None:
                    if schema.get("additionalProperties", True) is False:
                        return f"{spec_name} arg '{key}' unexpected property '{child_key}'"
                    child_schema = schema.get("additionalProperties")
                    if not isinstance(child_schema, Mapping):
                        continue
                error = check_schema_value(spec_name, f"{key}.{child_key}", child_schema, child_value, strict=strict)
                if error:
                    return error
    return ""


def _json_value_equal(left: object, right: object) -> bool:
    """JSON numbers compare numerically; booleans never compare as numbers."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, Mapping):
        if not isinstance(right, Mapping) or left.keys() != right.keys():
            return False
        left_mapping: Mapping[object, object] = left
        right_mapping: Mapping[object, object] = right
        return all(_json_value_equal(value, right_mapping[key]) for key, value in left_mapping.items())
    if isinstance(left, (list, tuple)):
        if not isinstance(right, (list, tuple)) or len(left) != len(right):
            return False
        left_items: Sequence[object] = left
        right_items: Sequence[object] = right
        return all(_json_value_equal(a, b) for a, b in zip(left_items, right_items, strict=True))
    return type(left) is type(right) and left == right


def check_schema_value(spec_name: str, key: str, schema: object, value: object, *, strict: bool = False) -> str:
    """声明 schema 子集的类型检查（非 Full JSON-schema）。"""
    if isinstance(schema, Mapping) and isinstance(schema.get("anyOf"), (list, tuple)):
        alternatives = schema["anyOf"]
        assert isinstance(alternatives, (list, tuple))
        if any(not check_schema_value(spec_name, key, item, value, strict=strict) for item in alternatives):
            return ""
        return f"{spec_name} arg '{key}' does not match any declared alternative"
    want, minimum, maximum = _spec_want_and_bounds(schema)
    if not want:
        return ""
    if isinstance(schema, Mapping) and "enum" in schema:
        enum = schema.get("enum")
        if isinstance(enum, (list, tuple)) and not any(_json_value_equal(value, item) for item in enum):
            return f"{spec_name} arg '{key}' must be one of {enum!r}"
    if want == "string":
        error = _check_string_value(spec_name, key, value)
        if error or not isinstance(schema, Mapping) or not isinstance(value, str):
            return error
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(schema.get("maxLength", len(value))):
            return f"{spec_name} arg '{key}' violates declared string length bounds"
        if "pattern" in schema:
            import re

            pattern = schema["pattern"]
            if not isinstance(pattern, str):
                return f"{spec_name} arg '{key}' has an invalid string pattern"
            if re.search(pattern, value) is None:
                return f"{spec_name} arg '{key}' must match {schema['pattern']}"
        return ""
    if want == "integer":
        return _check_integer_value(spec_name, key, value, minimum, maximum, strict=strict)
    if want == "number":
        return _check_number_value(spec_name, key, value, schema, strict=strict)
    if want == "boolean":
        return _check_boolean_value(spec_name, key, value)
    if want == "null":
        return "" if value is None else _spec_type_error(spec_name, key, "null", value)
    if want == "array":
        return _check_array_value(spec_name, key, schema, value, strict=strict)
    if want == "object":
        return _check_object_value(spec_name, key, schema, value, strict=strict)
    return ""
