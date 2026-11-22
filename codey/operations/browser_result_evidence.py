"""Derived browser action summaries and current-result checks over original receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping

from codey.automation.browser_document import same_document
from codey.operations.browser_completion import committed_results, complete_page_passages
from codey.operations.browser_followups import delivery, query_document
from codey.research.ledger_receipts import source_observation
from codey.runtime.core.execution_record import ExecutionRecord
from codey.runtime.core.models import ToolResult
from codey.toolchain.tool_spec import spec_for_tool
from codey.types.json_value import JsonObject, decode_json_object


def browser_actions(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult]) -> list[tuple[str, ToolResult]]:
    names = frozenset(row.call.name for row in results.values()
        if (spec := spec_for_tool(row.call.name)) is not None and spec.executor == "browser" and spec.effect == "page_mutation")
    return [(ref, row) for ref, row in committed_results(records, results, names, include_failed=names)
            if row.ok or delivery(row) in {"delivered", "verified", "uncertain"}]


def action_summary(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                   selected: tuple[str, ...] | None = None) -> tuple[str, tuple[str, ...]]:
    actions = browser_actions(records, results)
    if selected is not None:
        actions = [(ref, row) for ref, row in actions if ref in selected]
        if tuple(ref for ref, _row in actions) != selected:
            raise ValueError("browser action summary has no original ordered action receipts")
    if not actions:
        return "", ()
    positions = {ref: index for index, ref in enumerate(records)}
    snapshots = committed_results(records, results, frozenset({"browser_snapshot"}))
    lines = ["## 程序回执记录", f"页面动作 {len(actions)} 次，其中点击 {sum(row.call.name == 'browser_click' for _ref, row in actions)} 次。"]
    for index, (ref, row) in enumerate(actions, 1):
        target = row.call.args.get("ref", "")
        for snapshot_ref, snapshot in reversed(snapshots):
            if positions[snapshot_ref] >= positions[ref] or not same_document(
                    row.audit.get("browser_document"), snapshot.audit.get("browser_document")):
                continue
            value = snapshot.canonical.get("value")
            elements = decode_json_object(value).get("elements") if isinstance(value, dict) else None
            found: list[JsonObject] = [decode_json_object(element) for element in elements if isinstance(element, dict)
                     and element.get("ref") == target] if isinstance(elements, list) else []
            if found:
                target = found[0].get("name", target)
                break
        detail_value: JsonObject = {"tool": row.call.name, "target": target, "delivery": str(delivery(row) or "unconfirmed"),
                                   "result_ref": ref}
        detail = json.dumps(detail_value, ensure_ascii=False)
        fence = "`" * (1 + max((len(match[0]) for match in re.finditer(r"`+", detail)), default=0))
        lines.append(f"{index}. {fence} {detail} {fence}")
    return "\n".join(lines), tuple(ref for ref, _row in actions)


def current_result_passages(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult]) -> tuple[str, frozenset[str]]:
    document = query_document(results)
    actions = [(ref, row) for ref, row in browser_actions(records, results)
               if same_document(row.audit.get("browser_document"), document)]
    if not actions or document is None:
        return "", frozenset()
    positions = {ref: index for index, ref in enumerate(records)}
    latest = actions[-1][0]
    passages: set[str] = set()
    for ref, row in committed_results(records, results, frozenset({"browser_read"})):
        if positions[ref] > positions[latest] and same_document(row.audit.get("browser_document"), document):
            url = str(document.get("url", ""))
            passages.update(complete_page_passages(row, url) or _confirmed_region_passages(row, latest, url))
    return latest, frozenset(passages)


def _confirmed_region_passages(result: ToolResult, click: str, url: str) -> set[str]:
    region = result.audit.get("query_region")
    raw = result.canonical.get("read_passages")
    if (result.truncated or result.audit.get("query_click") != click or not isinstance(region, dict)
            or region.get("visible") is not True or not isinstance(raw, list)):
        return set()
    source, text, _ = source_observation(result.canonical.get("research_observation"))
    scope = source.read_scope
    if source.final_url != url or source.truncated or scope is None or scope.start is None or scope.end is None:
        return set()
    if " ".join(text[scope.start:scope.end].split()) != region.get("text"):
        return set()
    start = scope.start
    refs: set[str] = set()
    for item in raw:
        row = decode_json_object(item)
        lower, upper, ref = row.get("start"), row.get("end"), row.get("ref")
        if (type(lower) is not int or type(upper) is not int or not isinstance(ref, str)
                or not start <= lower < upper <= scope.end or text[start:lower].strip()):
            return set()
        refs.add(ref)
        start = upper
    return refs if not text[start:scope.end].strip() else set()


def validate_current_result(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                            markdown: str, state: str, aliases: tuple[tuple[str, str], ...]) -> None:
    action, current = current_result_passages(records, results)
    if not action:
        return
    labels = {label for label, passage in aliases if passage in current}
    from codey.research.answer_contract import citation_occurrences

    if state == "answered" and (not current or not any(row["label"] in labels for row in citation_occurrences(markdown))):
        raise ValueError("browser_result_evidence_missing: cite a complete formal browser_read after the latest page action. "
            "Earlier sources remain background evidence. Read the current result without repeating the action, "
            "or describe the actual acquisition gap as partial/unresolved.")
