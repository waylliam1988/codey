"""Ephemeral comparisons of committed receipts; never execution evidence."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from codey.operations.kernel_protocol import TurnSnapshot
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, decode_json_value, require_json_integer

# Repeated observations do not establish whether another effect was required
# or occurred. These calls may be reminded, but cannot alone justify stopping.
EFFECT_OBSERVATIONS = frozenset({"browser_click", "browser_fill", "browser_scroll", "browser_navigate",
    "browser_eval", "browser_upload", "browser_download", "browser_tab_new", "browser_tab_switch"})


def _encoded(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_value(result: ToolResult) -> JsonObject:
    from codey.research.ledger_receipts import source_observation

    source, text, _pages = source_observation(result.canonical["research_observation"])
    windows: list[JsonValue] = []
    passages = result.canonical.get("read_passages")
    if isinstance(passages, list):
        for item in passages:
            row = decode_json_object(item)
            start = require_json_integer(row["start"], "start")
            end = require_json_integer(row["end"], "end")
            windows.append({"start": start, "end": end, "text": text[start:end]})
    return {"url": source.final_url, "title": source.title, "windows": windows, "truncated": source.truncated}


def _browser_value(result: ToolResult) -> JsonObject | None:
    if result.call.name == "browser_read" and result.ok and "research_observation" in result.canonical:
        return _read_value(result)
    value = result.canonical.get("value")
    if not result.call.name.startswith("browser_") or not isinstance(value, dict):
        return None
    value = dict(value)
    # Only protocol-owned locations are normalized. In particular, eval.value
    # and delivered download artifacts remain complete business values.
    for key in ("ref", "generation", "page_id"):
        value.pop(key, None)
    if result.call.name == "browser_snapshot":
        elements = value.get("elements")
        if isinstance(elements, list):
            value["elements"] = [{key: item for key, item in row.items() if key != "ref"}
                                 if isinstance(row, dict) else row for row in elements]
        controls = value.get("file_controls")
        if isinstance(controls, list):
            value["file_controls"] = [{key: item for key, item in row.items() if key != "id"}
                                      if isinstance(row, dict) else row for row in controls]
            # File-only snapshot text is a rendering of those same controls.
            if value.get("scope") == "file_inputs":
                value.pop("text", None)
    elif result.call.name in {"browser_fill", "browser_navigate", "browser_eval", "browser_wait", "browser_tab_list",
                              "browser_tab_new", "browser_tab_switch", "browser_upload", "browser_download"}:
        # Execution fills missing structured text with model_text. It contains
        # ref/generation renderings rather than a separate page observation.
        value.pop("text", None)
    elif result.call.name in {"browser_click", "browser_scroll"} and value.get("observation") != "observed":
        value.pop("text", None)
    # A structured reason wins; retain otherwise unavailable error details.
    if not result.ok and "reason" not in value:
        value["error"] = result.model_text
    return value


def _target(args: JsonObject, receipts: Mapping[str, ToolResult]) -> JsonObject:
    ref = args.get("ref")
    if not isinstance(ref, str):
        return args
    for receipt in reversed(tuple(receipts.values())):
        if receipt.call.name != "browser_snapshot" or not receipt.ok:
            continue
        value = receipt.canonical.get("value")
        if not isinstance(value, dict) or value.get("elements_truncated") is not False or receipt.truncated:
            continue
        elements = value.get("elements")
        if not isinstance(elements, list):
            continue
        matches = [row for row in elements if isinstance(row, dict) and row.get("ref") == ref]
        if len(matches) != 1:
            continue
        row = matches[0]
        if sum(isinstance(item, dict) and (item.get("role"), item.get("name")) ==
               (row.get("role"), row.get("name")) for item in elements) != 1:
            return args
        return {**{key: item for key, item in args.items() if key != "ref"},
                "target": {"role": row.get("role"), "name": row.get("name"),
                           "page_url": value.get("page_url"), "page_id": value.get("page_id")}}
    return args


@dataclass(frozen=True)
class ProgressComparison:
    call: ToolCall
    result: ToolResult
    information: str


def progress_comparison(result: ToolResult, receipts: Mapping[str, ToolResult],
                        snapshot: TurnSnapshot | None) -> ProgressComparison:
    value = _browser_value(result)
    if value is not None:
        call = ToolCall(result.call.name, _target(result.call.args, receipts))
        text = _encoded({"ok": result.ok, "value": value, "truncated": result.truncated})
        return ProgressComparison(call, ToolResult(call, text, ok=result.ok, audit=result.audit), text)
    binding = snapshot.input_binding if snapshot is not None else None
    refs = result.canonical.get("child_effect_ids")
    if result.call.name == "codemode" and binding is not None and binding.kind == "actions" and isinstance(refs, list):
        children = [receipts[ref] for ref in refs if isinstance(ref, str) and ref in receipts]
        projected = [progress_comparison(child, receipts, None) for child in children]
        inputs = result.call.args.get("inputs", {})
        intent = decode_json_object(inputs)
        target = intent.get("target")
        if isinstance(target, dict) and "ref" in target:
            intent["target"] = _target(target, receipts)
        tool = children[-1].call.name if children else "codemode"
        call = ToolCall(tool, {"structured_inputs": intent,
            "operations": [{"tool": child.call.name, "args": child.call.args} for child in projected]})
        text = _encoded({"ok": result.ok, "truncated": result.truncated,
            "error_code": decode_json_value(result.audit.get("error_code")),
            "children": [{"tool": child.call.name, "ok": child.result.ok, "result": child.information}
                         for child in projected],
            "failure": result.model_text if not result.ok and not any(not child.ok for child in children) else None})
        return ProgressComparison(call, ToolResult(call, text, ok=result.ok, audit=result.audit), text)
    # General CodeMode return values retain their own semantics.
    return ProgressComparison(result.call, result, result.model_text)
