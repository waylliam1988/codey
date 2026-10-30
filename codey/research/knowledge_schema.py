"""Reusable nested schema values; complete tool contracts live in tool_contract."""

from typing import Any

from codey.knowledge.model import CONDITION_STATES, KINDS, ORIGINS, SCOPES, STATUSES


def text_schema(minimum: int = 1, maximum: int = 4000) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def object_schema(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def array_schema(items: dict[str, Any], minimum: int = 0, maximum: int = 32) -> dict[str, Any]:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def condition_map_schema() -> dict[str, Any]:
    return {"type": "object", "description": "Map condition names to {state,value?}; keep unknowns unknown.",
            "additionalProperties": object_schema({
                "state": {"type": "string", "enum": sorted(CONDITION_STATES)}, "value": text_schema()}, "state")}


def condition_array_schema() -> dict[str, Any]:
    return array_schema(object_schema({"name": text_schema(),
        "state": {"type": "string", "enum": sorted(CONDITION_STATES)}, "value": text_schema()}, "name", "state"), 0, 24)


def comparison_schema() -> dict[str, Any]:
    keys = ("subject", "factor", "comparator", "endpoint", "direction", "uncertainty")
    return object_schema({key: text_schema(1, 2000) for key in keys}, *keys)


def mutation_schema() -> dict[str, Any]:
    text = text_schema()
    reference = object_schema({"target": text, "revision": {"type": "integer", "minimum": 1},
                               "relation": text}, "target", "revision", "relation")
    value = object_schema({"id": text, "kind": {"type": "string", "enum": sorted(KINDS)},
        "content": {"type": "string", "minLength": 1}, "origin": {"type": "string", "enum": sorted(ORIGINS)},
        "title": text, "status": {"type": "string", "enum": sorted(STATUSES)},
        "scope": {"type": "string", "enum": sorted(SCOPES)}, "scope_id": {"type": "string"},
        "aliases": array_schema(text), "conditions": condition_map_schema(), "provenance": {"type": "object"},
        "references": array_schema(reference)}, "id", "kind", "content", "origin")
    return object_schema({"expected_revision": {"type": "integer", "minimum": 0}, "value": value},
                         "expected_revision", "value")


def note_evidence_schema() -> dict[str, Any]:
    return object_schema({"claim": text_schema(), "source_url": text_schema(), "excerpt": text_schema(1, 20000),
        "stance": {"type": "string", "enum": ["supports", "contradicts", "limits"]},
        "page": {"type": "integer", "minimum": 1}, "locator": text_schema()}, "claim", "source_url", "excerpt")


def coverage_record_schema() -> dict[str, Any]:
    from codey.reviews.research_coverage import QUESTION_STATES

    return object_schema({"question_ref": {"type": "string", "pattern": "^q[1-8]$"},
        "state": {"type": "string", "enum": list(QUESTION_STATES)}, "answer": text_schema(),
        "findings": array_schema({"type": "string", "pattern": "^f[1-9][0-9]*$"}, 0, 16),
        "investigations": array_schema({"type": "string", "pattern": "^a[1-9][0-9]*$"})},
        "question_ref", "state", "answer", "findings", "investigations")


def report_sections_schema() -> dict[str, Any]:
    fields = {key: array_schema(text_schema(1, 16000), 1) for key in ("evidence", "counter", "source_quality")}
    fields["coverage"] = array_schema(coverage_record_schema(), 1, 8)
    return object_schema(fields, *fields)


def knowledge_reference_schema() -> dict[str, Any]:
    return object_schema({"id": text_schema(), "revision": {"type": "integer", "minimum": 1}}, "id", "revision")
