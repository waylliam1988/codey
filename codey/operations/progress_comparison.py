"""Ephemeral comparisons of committed receipts; never execution evidence."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from codey.automation.browser_document import same_document
from codey.operations.browser_followups import NAVIGATE
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import spec_for_tool
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
        # A full observation advances the transport revision even when its
        # document and visible content are unchanged. It is not new evidence.
        if value.get("kind") == "full":
            value.pop("revision", None)
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
    reloaded: bool = False


def _program_children(result: ToolResult, receipts: Mapping[str, ToolResult]) -> tuple[tuple[str, ToolResult], ...] | None:
    """Only the complete ordered children of this original parent can be projected."""
    canonical = decode_json_object(result.canonical)
    refs = canonical.get("child_effect_ids")
    parent = result.audit.get("codemode_execution_id")
    if (result.call.name != "codemode" or not result.ok or result.truncated
            or not isinstance(parent, str) or not parent or not isinstance(refs, list) or not refs
            or result.audit.get("child_calls") != len(refs) or "value" not in result.canonical):
        return None
    children: list[tuple[str, ToolResult]] = []
    for index, ref in enumerate(refs):
        if not isinstance(ref, str) or (child := receipts.get(ref)) is None or child.call.call_id != f"{parent}:child:{index}":
            return None
        children.append((ref, child))
    owned = {ref for ref, child in receipts.items() if child.call.call_id.startswith(parent + ":child:")}
    if {ref for ref, _child in children} != owned:
        return None
    return tuple(children)


def _observation_program(result: ToolResult, receipts: Mapping[str, ToolResult]) -> str | None:
    """Compare observed children, preserving arbitrary computed business values."""
    rows = _program_children(result, receipts)
    if rows is None:
        return None
    children: list[JsonValue] = []
    substitutions: dict[str, str] = {}
    for index, (_ref, child) in enumerate(rows):
        if (not child.ok or child.truncated
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


def reload_snapshot(result: ToolResult, result_ref: str) -> JsonObject | None:
    """Compare a complete observation in a document proven to come from the fixed wrapper."""
    from codey.operations.browser_references import BrowserReferences

    raw = result.canonical.get("value")
    if not isinstance(raw, dict):
        return None
    raw = decode_json_object(raw)
    if (result.call.name != "browser_snapshot" or not result.ok or result.truncated
            or raw.get("kind") != "full" or raw.get("complete") is not True
            or raw.get("text_truncated") is not False or not isinstance(document := raw.get("document"), dict)
            or not same_document(document, result.audit.get("browser_document"))):
        return None
    contexts, elements = raw.get("contexts"), raw.get("elements")
    if not isinstance(contexts, list) or not isinstance(elements, list):
        return None
    document = decode_json_object(document)
    try:
        BrowserReferences.observe(result_ref, raw, document)
    except ValueError:
        return None
    observed = _browser_value(result)
    if observed is None:
        return None
    # Reloading changes the loader and transport references. The complete visible
    # observation, target/frame/URL and all business fields still participate.
    observed["document"] = {key: value for key, value in document.items() if key != "loader_id"}
    aliases: dict[str, str] = {}
    normalized_contexts: list[JsonValue] = []
    for index, value in enumerate(contexts):
        row = decode_json_object(value)
        alias = f"context-{index}"
        aliases[str(row["id"])] = alias
        normalized_contexts.append({**row, "id": alias})
    normalized_elements: list[JsonValue] = []
    for value in elements:
        row = decode_json_object(value)
        context_id = str(row.get("context_id", ""))
        normalized_elements.append({**{key: item for key, item in row.items() if key != "ref"},
                                    "context_id": aliases.get(context_id, context_id)})
    observed["contexts"], observed["elements"] = normalized_contexts, normalized_elements
    return observed


def _navigation_program(result: ToolResult, receipts: Mapping[str, ToolResult]) -> ProgressComparison | None:
    """Project the shared fixed navigation wrapper, never arbitrary guest programs."""
    if result.call.args.get("code") != NAVIGATE or (rows := _program_children(result, receipts)) is None or len(rows) != 2:
        return None
    navigation, observation = rows[0][1], rows[1][1]
    if (navigation.call.name != "browser_navigate" or not navigation.ok or navigation.truncated
            or result.canonical.get("value") != navigation.canonical.get("value")
            or not same_document(observation.audit.get("browser_document"), navigation.audit.get("browser_document"))):
        return None
    observed, action = reload_snapshot(observation, rows[1][0]), _browser_value(navigation)
    if observed is None or action is None:
        return None
    call = ToolCall("browser_navigate", navigation.call.args)
    text = _encoded({"ok": True, "navigation": action, "observation": observed})
    return ProgressComparison(call, ToolResult(call, text, ok=True, audit=result.audit), text, reloaded=True)


def _navigation_snapshot(result: ToolResult, receipts: Mapping[str, ToolResult]) -> JsonObject | None:
    """Keep an independent read after a known reload in the same comparison domain."""
    if result.call.name != "browser_snapshot" or result.call.args:
        return None
    result_ref = next((ref for ref, row in receipts.items() if row is result), None)
    if result_ref is None:
        return None
    for parent in reversed(tuple(receipts.values())):
        if parent.call.name != "codemode" or parent.call.args.get("code") != NAVIGATE:
            continue
        rows = _program_children(parent, receipts)
        if (rows is not None and len(rows) == 2
                and same_document(result.audit.get("browser_document"), rows[1][1].audit.get("browser_document"))
                and _navigation_program(parent, receipts) is not None):
            order = tuple(receipts)
            before, after = order.index(rows[1][0]), order.index(result_ref)
            if before >= after or any((spec := spec_for_tool(receipts[ref].call.name)) is not None
                    and spec.executor == "browser" and spec.effect != "read" for ref in order[before + 1:after]):
                return None
            return reload_snapshot(result, result_ref)
    return None


def progress_comparison(result: ToolResult, receipts: Mapping[str, ToolResult]) -> ProgressComparison:
    value = _navigation_snapshot(result, receipts)
    reloaded = value is not None
    if value is None:
        value = _browser_value(result)
    if value is not None:
        call = ToolCall(result.call.name, _target(result.call.args, receipts))
        text = _encoded({"ok": result.ok, "value": value, "truncated": result.truncated})
        return ProgressComparison(call, ToolResult(call, text, ok=result.ok, audit=result.audit), text,
                                  result.ok and result.call.name in {"browser_snapshot", "browser_read"}, reloaded)
    if (navigation := _navigation_program(result, receipts)) is not None:
        return navigation
    if (observed := _observation_program(result, receipts)) is not None:
        return ProgressComparison(result.call, ToolResult(result.call, observed, ok=result.ok, audit=result.audit), observed, True)
    # General CodeMode return values retain their own semantics.
    return ProgressComparison(result.call, result, result.model_text)
