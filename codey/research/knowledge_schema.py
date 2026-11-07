"""Reusable nested schema values; complete tool contracts live in tool_contract."""

from collections.abc import Mapping

from codey.knowledge.model import CONDITION_STATES, KINDS, ORIGINS, SCOPES, STATUSES
from codey.reviews.research_coverage import QUESTION_STATES
from codey.types.json_value import JsonObject, canonical_json_object


def text_schema(minimum: int = 1, maximum: int = 4000) -> JsonObject:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def object_schema(properties: Mapping[str, object], *required: str) -> JsonObject:
    return canonical_json_object({"type": "object", "properties": properties, "required": list(required), "additionalProperties": False})


def array_schema(items: JsonObject, minimum: int = 0, maximum: int = 32) -> JsonObject:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def condition_map_schema() -> JsonObject:
    return {"type": "object", "description": "Map condition names to {state,value?}; keep unknowns unknown.",
            "additionalProperties": object_schema({
                "state": {"type": "string", "enum": sorted(CONDITION_STATES)}, "value": text_schema()}, "state")}


def condition_array_schema() -> JsonObject:
    return array_schema(object_schema({"name": text_schema(),
        "state": {"type": "string", "enum": sorted(CONDITION_STATES)}, "value": text_schema()}, "name", "state"), 0, 24)


def comparison_schema() -> JsonObject:
    keys = ("subject", "factor", "comparator", "endpoint", "direction", "uncertainty")
    return object_schema({key: text_schema(1, 2000) for key in keys}, *keys)


def mutation_schema() -> JsonObject:
    text = text_schema()
    reference = object_schema({"target": text, "revision": {"type": "integer", "minimum": 1},
                               "relation": text}, "target", "revision", "relation")
    value = object_schema({"id": text, "kind": {"type": "string", "enum": sorted(KINDS)},
        "content": {"type": "string", "minLength": 1}, "origin": {"type": "string", "enum": sorted(ORIGINS)},
        "title": text_schema(0), "status": {"type": "string", "enum": sorted(STATUSES)},
        "scope": {"type": "string", "enum": sorted(SCOPES)}, "scope_id": {"type": "string"},
        "aliases": array_schema(text), "conditions": condition_map_schema(), "provenance": {"type": "object"},
        "references": array_schema(reference)}, "id", "kind", "content", "origin")
    return object_schema({"expected_revision": {"type": "integer", "minimum": 0}, "value": value},
                         "expected_revision", "value")


def note_evidence_schema() -> JsonObject:
    return object_schema({"claim": text_schema(), "source_url": text_schema(), "excerpt": text_schema(1, 20000),
        "stance": {"type": "string", "enum": ["supports", "contradicts", "limits"]},
        "page": {"type": "integer", "minimum": 1}, "locator": text_schema()}, "claim", "source_url", "excerpt")


def coverage_record_schema() -> JsonObject:
    return object_schema({"question_ref": {"type": "string", "pattern": "^q[1-8]$"},
        "state": {"type": "string", "enum": list(QUESTION_STATES)}, "answer": text_schema(),
        "findings": array_schema({"type": "string", "pattern": "^f[1-9][0-9]*$"}, 0, 16),
        "investigations": array_schema({"type": "string", "pattern": "^a[1-9][0-9]*$"}),
        "task_refs": array_schema({"type": "string", "pattern": "^r[1-9][0-9]*$"}, 0, 512)},
        "question_ref", "state", "answer", "findings", "investigations")


def report_sections_schema() -> JsonObject:
    fields = {key: array_schema(text_schema(1, 16000), 0 if key == "counter" else 1)
              for key in ("evidence", "counter")}
    fields["coverage"] = array_schema(coverage_record_schema(), 1, 8)
    return object_schema(fields, *fields)


def knowledge_reference_schema() -> JsonObject:
    return object_schema({"id": text_schema(), "revision": {"type": "integer", "minimum": 1}}, "id", "revision")


def schema_object(schema: JsonObject, key: str) -> JsonObject:
    value = schema[key]
    if not isinstance(value, dict):
        raise ValueError(f"schema {key} must be an object")
    return value


def schema_strings(schema: JsonObject, key: str) -> list[str]:
    value = schema[key]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"schema {key} must be strings")
    return [str(item) for item in value]
