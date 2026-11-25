"""Receipt facts shared by browser orchestration and research."""
from __future__ import annotations

from collections.abc import Mapping

from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, JsonValue, decode_json_object


def committed_results(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                      names: frozenset[str], *, include_failed: frozenset[str] = frozenset()) -> list[tuple[str, ToolResult]]:
    return [(ref, result) for ref, row in records.items() if row["name"] in names
            and (row["ok"] or row["name"] in include_failed) and row.get("execution_disposition") != "denied_before_execution"
            and (result := results.get(ref)) is not None and result.ok == row["ok"] and result.call.name == row["name"]
            and result.call.call_id == row["call_id"]]


def delivery(result: ToolResult) -> object:
    value = result.canonical.get("value")
    return value.get("delivery") if isinstance(value, dict) else None


def query_document(results: Mapping[str, ToolResult], document: JsonObject | None = None) -> JsonObject | None:
    if document is not None:
        return document
    for receipt in reversed(tuple(results.values())):
        observed = receipt.audit.get("browser_document")
        if isinstance(observed, dict):
            return decode_json_object(observed)
    return None


def operation_receipts(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult]) -> list[JsonObject]:
    """Bounded action status from settled receipts, never another state store.

    Page observations are task progress, not research evidence. In particular,
    omit DOM text, script output and filled values from this operational view.
    """
    rows: list[JsonObject] = []
    for identity, result in results.items():
        spec = spec_for_tool(result.call.name)
        if identity not in records or spec is None or spec.executor != "browser":
            continue
        value = result.canonical.get("value")
        status: JsonObject = value if isinstance(value, dict) else dict[str, JsonValue]()
        row: JsonObject = {"result_ref": identity, "tool": result.call.name, "ok": result.ok}
        for key in ("action", "delivery", "page_id", "page_url", "generation", "reason",
                    "matched", "effect_verified", "value_length", "name", "bytes", "sha256",
                    "suggested_filename", "handle", "path"):
            if key in status:
                row[key] = status[key]
        rows.append(row)
    return rows[-12:]
