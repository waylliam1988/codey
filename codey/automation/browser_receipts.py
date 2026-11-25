"""Receipt facts shared by browser orchestration and research."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from codey.policies.browser_requirement import PAGE_TRANSITIONS
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, JsonValue, decode_json_object

PAGE_EVIDENCE_TOOLS = frozenset({"browser_wait", "browser_read"})
FORM_ACTIONS = frozenset({"browser_fill", "browser_click"})
PAGE_ACTIONS = PAGE_TRANSITIONS | FORM_ACTIONS | {"browser_upload"}
PAGE_EVIDENCE_INVALIDATORS = PAGE_ACTIONS | {"browser_eval", "browser_scroll", "browser_download"}


@dataclass(frozen=True)
class OrderedBrowserProgress:
    ready: bool = False
    uncertain: bool = False
    refs: frozenset[str] = frozenset()
    prerequisite_refs: frozenset[str] = frozenset()


def page_effect_possible(result: ToolResult) -> bool:
    """Only an explicit pre-delivery rejection proves the page was untouched."""
    value = result.canonical.get("value")
    if result.ok or not isinstance(value, dict):
        return True
    action: Mapping[str, object] = value
    return action.get("delivery") != "not_delivered"


def ordered_browser_progress(tool: str, after: str, *, records: Mapping[str, ExecutionRecord],
                             results: Mapping[str, ToolResult], document: JsonObject | None,
                             stage: int = 0) -> OrderedBrowserProgress:
    """Derive prerequisite readiness and eligible evidence in one receipt pass."""
    if document is None:
        return OrderedBrowserProgress()
    prerequisites = PAGE_ACTIONS if after == "page_actions" else frozenset({after})
    eligible: set[str] = set()
    ready = False
    uncertain = False
    click_stage = 0
    clicks: set[str] = set()
    prerequisite_refs: frozenset[str] = frozenset()
    for ref, result in committed_results(records, results, prerequisites | {tool, "browser_click"},
                                         include_failed=prerequisites | {"browser_click"}):
        if result.audit.get("browser_document") != document:
            continue
        if result.call.name in PAGE_EVIDENCE_INVALIDATORS and not page_effect_possible(result):
            continue
        if _stage_click(result):
            click_stage += 1
            clicks.add(ref)
        if stage and click_stage != stage:
            continue
        if result.call.name in prerequisites:
            eligible.clear()
            value = result.canonical.get("value")
            action = decode_json_object(value) if isinstance(value, dict) else {}
            delivered = action.get("delivery")
            uncertain = delivered == "uncertain"
            ready = ((result.ok or delivered == "delivered") and not uncertain if after == "page_actions" else
                     result.ok and wait_matched_on_page(result, str(document.get("url", ""))))
            prerequisite_refs = frozenset({ref}) if ready else frozenset()
            if ready and stage:
                prerequisite_refs |= clicks
        elif result.call.name == "browser_click":
            eligible.clear()
            ready = False
            prerequisite_refs = frozenset()
        elif result.call.name == tool and ready and result.ok:
            if tool != "browser_wait" or wait_matched_on_page(result, str(document.get("url", ""))):
                eligible.add(ref)
    return OrderedBrowserProgress(ready, uncertain, frozenset(eligible), prerequisite_refs)


def browser_click_stage(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                        document: JsonObject | None) -> int:
    return sum(result.audit.get("browser_document") == document and _stage_click(result)
        for _, result in committed_results(
        records, results, frozenset({"browser_click"}), include_failed=frozenset({"browser_click"})))


def _stage_click(result: ToolResult) -> bool:
    value = result.canonical.get("value")
    return result.call.name == "browser_click" and (result.ok or isinstance(value, dict)
                                                    and value.get("delivery") in {"delivered", "uncertain"})


def wait_matched_on_page(result: ToolResult, url: str) -> bool:
    observed = result.canonical.get("value")
    if not isinstance(observed, dict):
        return False
    value = dict[str, object](observed)
    return value.get("matched") is True and value.get("page_url") == url


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
