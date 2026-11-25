"""Model declarations for exact arithmetic over captured receipts."""
from __future__ import annotations

from codey.research.knowledge_schema import array_schema, object_schema
from codey.types.json_value import JsonObject

OPERATIONS = ("sum", "subtract", "multiply", "divide", "percentage", "growth_rate", "percentage_points", "mean", "weighted_mean", "convert", "count")
_OPERAND = object_schema({"result_ref": {"type": "string"}, "pointer": {"type": "string", "maxLength": 2048}}, "result_ref", "pointer")
_TABLE = object_schema({"result_ref": {"type": "string"}, "value_column": {"type": "string"},
    "weight_column": {"type": "string"}, "where": object_schema({"column": {"type": "string"},
        "equals": {"type": "string"}}, "column", "equals")}, "result_ref", "value_column", "where")


def data_schema() -> JsonObject:
    return object_schema({"evidence": {"type": "string"}, "result_ref": {"type": "string"}, "path": {"type": "string"},
        "format": {"type": "string", "enum": ["quantities", "csv", "json"]}}, "format")


def calculation_schema() -> JsonObject:
    return object_schema({"operation": {"type": "string", "enum": list(OPERATIONS)},
        "operands": array_schema(_OPERAND, 1, 1000), "weights": array_schema(_OPERAND, 1, 1000),
        "table": _TABLE,
        "unit": {"type": "string"}, "scale": {"type": "integer", "minimum": 0, "maximum": 12},
        "missing": {"type": "string", "enum": ["error", "skip"]}}, "operation")


def data_output_schema() -> JsonObject:
    quantity = object_schema({"value": {"type": "string"}, "unit": {"type": "string"},
        "start": {"type": "integer"}, "end": {"type": "integer"}, "context": {"type": "string"}},
        "value", "unit", "start", "end", "context")
    return {"anyOf": [object_schema({"format": {"type": "string", "enum": [name]}, "data": data,
        "source_ref": {"type": "string"}, "source_version": {"type": "string"},
        "passage_refs": {"type": "array", "items": {"type": "string"}}},
        "format", "data", "source_ref", "source_version", "passage_refs") for name, data in (
            ("quantities", {"type": "array", "items": quantity}),
            ("csv", {"type": "array", "items": {"type": "object", "additionalProperties": {"type": "string"}}}),
            ("json", JsonObject()))]}


def calculation_output_schema() -> JsonObject:
    fields: JsonObject = {key: {"type": "string"} for key in ("operation", "value", "unit", "rounding", "validation")}
    fields.update({"scale": {"type": "integer"}, "missing_skipped": {"type": "integer"},
        "inputs": {"type": "array", "items": _OPERAND},
        "passage_refs": {"type": "array", "items": {"type": "string"}}})
    return object_schema(fields, *fields)
