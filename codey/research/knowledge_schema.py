"""Shared wire declarations for knowledge tools; repository validates semantics."""

from typing import Any

from codey.knowledge.model import CONDITION_STATES, KINDS, ORIGINS, SCOPES, STATUSES


def _object(properties: dict[str, Any], *required: str) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _array(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def parameter_schemas(tool: str) -> dict[str, Any]:
    text: dict[str, Any] = {"type": "string"}
    conditions = {"type": "object", "description": 'Map condition names to objects, e.g. '
                  '{"journal_mode":{"state":"reported","value":"WAL"},'
                  '"workload":{"state":"not_extracted"}}. Never a flat name/state/value object.',
                  "additionalProperties": _object({
        "state": {"type": "string", "enum": sorted(CONDITION_STATES)}, "value": text}, "state")}
    reference = _object({"target": text, "revision": {"type": "integer", "minimum": 1}, "relation": text},
                        "target", "revision", "relation")
    if tool == "knowledge_commit":
        value = _object({
            "id": text, "kind": {"type": "string", "enum": sorted(KINDS)}, "content": text,
            "origin": {"type": "string", "enum": sorted(ORIGINS)}, "title": text,
            "status": {"type": "string", "enum": sorted(STATUSES)}, "scope": {"type": "string", "enum": sorted(SCOPES)},
            "scope_id": text, "aliases": _array(text), "conditions": conditions,
            "provenance": {"type": "object"}, "references": _array(reference),
        }, "id", "kind", "content", "origin")
        return {"changes": _array(_object({"expected_revision": {"type": "integer", "minimum": 0}, "value": value},
                                           "expected_revision", "value"))}
    if tool == "knowledge_write":
        from codey.knowledge.note import NOTE_TYPES

        # Preserve the ledger's legacy field aliases and optional fallback claim.
        evidence = _object({"claim": text, "source_url": text, "source": text, "excerpt": text, "quote": text,
                            "stance": text, "page": {"type": "integer", "minimum": 1},
                            "p": {"type": "integer", "minimum": 1}, "locator": text})
        return {"body": {"type": "string", "description": "Required note text, even when evidence is supplied."},
                "id": {"type": "string", "description": "Optional existing note_id to update, exactly as returned by knowledge_write. Omit for a new note; never invent it or use a structured object id."},
                "type": {"type": "string", "enum": list(NOTE_TYPES)},
                "sources": _array(text), "tags": _array(text), "aliases": _array(text),
                "evidence": {**_array(evidence), "description": "Required for sourced findings. Each excerpt must be an exact contiguous passage from an opened source; never a page title or navigation text."},
                "conditions": conditions}
    if tool == "done":
        from codey.reviews.report_sections import REQUIRED_SECTIONS

        keys = REQUIRED_SECTIONS[1:]
        return {"knowledge_refs": _array(_object({"id": text, "revision": {"type": "integer", "minimum": 1}},
                                                  "id", "revision")),
                "report_sections": _object({key: _array(text) for key in keys}, *keys)}
    return {}
