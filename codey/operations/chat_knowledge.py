"""Project Chat citation choices from committed knowledge-read receipts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from codey.operations.browser_completion import committed_results
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, decode_json_value


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
