"""Ephemeral comparisons of committed receipts; never execution evidence."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from codey.automation.browser_document import same_document
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_integer

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
    raw = result.canonical.get("value")
    if not result.call.name.startswith("browser_") or not isinstance(raw, dict):
        return None
    value = decode_json_object(raw)
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
        raw = receipt.canonical.get("value")
        if not isinstance(raw, dict) or receipt.truncated:
            continue
        value = decode_json_object(raw)
        if value.get("elements_truncated") is not False:
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
    browser_observation: bool = False


def _observation_program(result: ToolResult, receipts: Mapping[str, ToolResult]) -> str | None:
    """Compare complete observed children and only their known ref substitutions.

    This is progress telemetry, never replay or repair authority. In particular,
    arbitrary computed values, numeric generations and unknown children survive.
    """
    canonical = decode_json_object(result.canonical)
    refs = canonical.get("child_effect_ids")
    parent = result.audit.get("codemode_execution_id")
    if (result.call.name != "codemode" or not result.ok or result.truncated
            or not isinstance(parent, str) or not parent or not isinstance(refs, list) or not refs
            or result.audit.get("child_calls") != len(refs) or "value" not in result.canonical):
        return None
    owned = {ref for ref, child in receipts.items() if child.call.call_id.startswith(parent + ":child:")}
    if any(not isinstance(ref, str) for ref in refs) or set(refs) != owned or len(owned) != len(refs):
        return None
    children: list[JsonValue] = []
    substitutions: dict[str, str] = {}
    for index, ref in enumerate(refs):
        child = receipts[str(ref)]
        if (child.call.call_id != f"{parent}:child:{index}" or not child.ok or child.truncated
                or child.call.name not in {"browser_snapshot", "browser_read"}
                or (value := _browser_value(child)) is None
                or not isinstance(document_raw := child.audit.get("browser_document"), dict)):
            return None
        document = decode_json_object(document_raw)
        if not same_document(document, document):
            return None
        if child.call.name == "browser_snapshot":
            raw = decode_json_object(child.canonical).get("value")
            if (not isinstance(raw, dict) or raw.get("complete") is not True
                    or raw.get("elements_truncated") is not False or raw.get("text_truncated") is not False
                    or not isinstance(elements := raw.get("elements"), list)):
                return None
            for position, element in enumerate(elements):
                if not isinstance(element, dict) or not isinstance(token := element.get("ref"), str) or not token:
                    return None
                replacement = f"<observed-control:{index}:{position}>"
                if token in substitutions and substitutions[token] != replacement:
                    return None
                substitutions[token] = replacement
        children.append({"tool": child.call.name, "args": child.call.args, "document": document, "value": value})
    if not substitutions:
        return None
    # A literal also present in the program/input may be business data. Do not
    # reinterpret it, or strings containing a ref as only part of an identifier.
    arguments = _encoded(result.call.args)
    if any(token in arguments for token in substitutions):
        return None
    pattern = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(
        re.escape(token) for token in sorted(substitutions, key=len, reverse=True)) + r")(?![A-Za-z0-9_])")

    def normalized(value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            # Encode text and observed references as distinct parts. A business
            # string equal to a runtime placeholder must not collide with it.
            parts: list[JsonValue] = []
            start = 0
            for match in pattern.finditer(value):
                parts.extend([value[start:match.start()], {"observed_ref": substitutions[match.group()]}])
                start = match.end()
            parts.append(value[start:])
            return _encoded(parts)
        if isinstance(value, list):
            return [normalized(item) for item in value]
        if isinstance(value, dict):
            return {key: normalized(item) for key, item in value.items()}
        return value

    return _encoded({"ok": True, "children": children, "value": normalized(
        decode_json_object(result.canonical)["value"])})


def progress_comparison(result: ToolResult, receipts: Mapping[str, ToolResult]) -> ProgressComparison:
    value = _browser_value(result)
    if value is not None:
        call = ToolCall(result.call.name, _target(result.call.args, receipts))
        text = _encoded({"ok": result.ok, "value": value, "truncated": result.truncated})
        return ProgressComparison(call, ToolResult(call, text, ok=result.ok, audit=result.audit), text)
    if (observed := _observation_program(result, receipts)) is not None:
        return ProgressComparison(result.call, ToolResult(result.call, observed, ok=result.ok, audit=result.audit), observed, True)
    # General CodeMode return values retain their own semantics.
    return ProgressComparison(result.call, result, result.model_text)
