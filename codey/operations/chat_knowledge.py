"""Project completion citation choices from committed knowledge-read receipts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from codey.knowledge.model import canonical_json
from codey.operations.browser_completion import committed_results
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, decode_json_value

HYBRID_KNOWLEDGE_GUIDANCE = (
    "knowledge_refs cites saved knowledge objects actually adopted in the answer, using exact id/revision pairs. "
    "A file read or edit does not create a knowledge object; browser source locators are separate from knowledge ids. "
    "Committed structured-object reads below are non-exhaustive choices, not automatic adoption. "
    "Legacy-note candidates and newly saved objects retain their existing identity, version and access checks; "
    "they are not established as direct reads by this list. "
)


def read_knowledge_versions(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult]) -> list[JsonObject]:
    versions: list[JsonObject] = []
    for _, receipt in committed_results(records, results, frozenset({"knowledge_read"})):
        raw = decode_json_value(receipt.canonical.get("knowledge_read_versions"))
        if not isinstance(raw, list):
            continue
        for value in raw:
            row = decode_json_object(value)
            if row not in versions:
                versions.append(row)
    return versions


def chat_knowledge_spec(spec: ToolSpec, versions: list[JsonObject]) -> ToolSpec:
    parameters: list[tuple[str, object]] = []
    for key, schema in spec.parameters:
        if key != "knowledge_refs":
            parameters.append((key, schema))
        elif versions:
            bound = canonical_json_object(schema)
            item = decode_json_object(bound["items"])
            properties = decode_json_object(item["properties"])
            bound["items"] = {"anyOf": [{**item, "properties": {
                key: {**decode_json_object(properties[key]), "enum": [value]} for key, value in version.items()
            }} for version in versions]}
            parameters.append((key, bound))
    return freeze_spec_parameters(replace(spec, parameters=tuple(parameters)))


def hybrid_knowledge_spec(spec: ToolSpec, versions: list[JsonObject], *, knowledge_required: bool) -> ToolSpec:
    """Offer receipt-backed pairs without closing Hybrid's broader citation contract."""
    guidance = HYBRID_KNOWLEDGE_GUIDANCE
    guidance += ("This task requires knowledge validation; do not omit adopted references to evade it. "
                 if knowledge_required else
                 "If no saved knowledge was used, omit this optional field. ")
    guidance += "Committed structured-object reads: " + canonical_json(versions)
    parameters = tuple((key, {**canonical_json_object(schema), "description": guidance}
                        if key == "knowledge_refs" else schema) for key, schema in spec.parameters)
    return freeze_spec_parameters(replace(spec, parameters=parameters))
