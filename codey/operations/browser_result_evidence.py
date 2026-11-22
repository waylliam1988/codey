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
from codey.types.json_value import JsonObject, decode_json_object, require_json_integer, require_json_string
from codey.utils.citation_scanner import mask_markdown_code


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
            passages.update(complete_page_passages(row, str(document.get("url", ""))))
    return latest, frozenset(passages)


def validate_current_result(records: Mapping[str, ExecutionRecord], results: Mapping[str, ToolResult],
                            markdown: str, state: str, aliases: tuple[tuple[str, str], ...]) -> None:
    action, current = current_result_passages(records, results)
    if not action:
        return
    labels = {label for label, passage in aliases if passage in current}
    if state == "answered" and (not current or not any(f"[{label}]" in markdown for label in labels)):
        raise ValueError("browser_result_evidence_missing: cite a complete formal browser_read after the latest page action. "
            "Earlier sources remain background evidence. Read the current result without repeating the action, "
            "or describe the actual acquisition gap as partial/unresolved.")
    texts: dict[str, str] = {}
    for row in results.values():
        passages = row.canonical.get("read_passages")
        if not row.ok or not isinstance(passages, list) or "research_observation" not in row.canonical:
            continue
        _source, text, _pages = source_observation(row.canonical["research_observation"])
        for raw in passages:
            passage = decode_json_object(raw)
            start, end = require_json_integer(passage["start"], "start"), require_json_integer(passage["end"], "end")
            texts[require_json_string(passage["ref"], "passage ref")] = " ".join(text[start:end].split())
    by_label = dict(aliases)
    prose = mask_markdown_code(markdown)
    patterns = (r'["“「](?P<quote>[^"”」\n]+)["”」][\s*_，。,.;:：]*\[(?P<label>e[1-9][0-9]*)\]',
                r'\[(?P<label>e[1-9][0-9]*)\][ \t*_：:]*["“「](?P<quote>[^"”」\n]+)["”」]')
    for pattern in patterns:
        for match in re.finditer(pattern, prose):
            label = require_json_string(match.group("label"), "quote label")
            quote = " ".join(require_json_string(match.group("quote"), "quote text").split())
            if quote not in texts.get(by_label.get(label, ""), ""):
                raise ValueError(f"browser_quote_not_in_source: the explicit quotation for [{label}] is absent from its passage. "
                    "Repair the quotation or cite the actual post-action source; do not repeat the page action.")
