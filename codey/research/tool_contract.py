"""Typed Research tool contracts for the text JSON fallback."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_NO_JSON = "no_json"
PROTOCOL_UNKNOWN_TOOL = "unknown_tool"
PROTOCOL_TOO_MANY_TOOLS = "too_many_tools"
PROTOCOL_INVALID_ARGS = "invalid_args"
PROTOCOL_DIRECT_ANSWER = "direct_answer"
PROTOCOL_NATIVE_SEARCH_LEAK = "native_search_leak"
PROTOCOL_DISALLOWED_TOOL = "disallowed_tool"


@dataclass(frozen=True)
class ToolArg:
    type: type
    default: Any = None
    singleton_dict: bool = False
    list_item_type: type | None = None


@dataclass(frozen=True)
class ToolContract:
    name: str
    required: dict[str, type] = field(default_factory=dict)
    optional: dict[str, ToolArg] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    args: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""


TOOL_CONTRACTS = {
    "web_search": ToolContract(
        name="web_search",
        required={"query": str},
        aliases={"queries": "query"},
    ),
    "open_url": ToolContract(
        name="open_url",
        required={"url": str},
        optional={
            "offset": ToolArg(int, 0),
            "limit": ToolArg(int, 6000),
            "pages": ToolArg(str, ""),
        },
    ),
    "source_search": ToolContract(
        name="source_search",
        required={"url": str, "query": str},
        optional={"limit": ToolArg(int, 6)},
        aliases={"queries": "query"},
    ),
    "knowledge_search": ToolContract(
        name="knowledge_search",
        required={"query": str},
        aliases={"queries": "query"},
    ),
    "knowledge_read": ToolContract(
        name="knowledge_read",
        required={"id": str},
        aliases={"note_id": "id"},
    ),
    "knowledge_write": ToolContract(
        name="knowledge_write",
        required={"type": str, "title": str, "body": str},
        optional={
            "id": ToolArg(str, ""),
            "sources": ToolArg(list, None),
            "tags": ToolArg(list, None),
            "aliases": ToolArg(list, None),
            "relations": ToolArg(list, None, singleton_dict=True, list_item_type=dict),
            "evidence": ToolArg(list, None, singleton_dict=True, list_item_type=dict),
            "confidence": ToolArg(float, None),
            "retrieved_at": ToolArg(str, None),
            "valid_until": ToolArg(str, None),
            "status": ToolArg(str, "active"),
        },
    ),
    "knowledge_link": ToolContract(
        name="knowledge_link",
        required={"src": str, "dst": str},
        optional={"kind": ToolArg(str, "relates")},
    ),
    "done": ToolContract(
        name="done",
        required={"answer": str},
        aliases={"summary": "answer", "text": "answer"},
    ),
}


def validate_tool_args(tool: str, args: dict[str, Any]) -> ContractResult:
    contract = TOOL_CONTRACTS.get(tool)
    if contract is None:
        return ContractResult(False, error=f"unknown tool: {tool}", error_kind=PROTOCOL_UNKNOWN_TOOL)
    normalized = _apply_aliases(args, contract.aliases)
    output: dict[str, Any] = {}
    for name, expected in contract.required.items():
        if name not in normalized:
            return _invalid(f"{tool} missing required arg '{name}'")
        coerced = _coerce(tool, name, normalized[name], expected, required=True)
        if not coerced.ok:
            return coerced
        output[name] = coerced.args[name]
    for name, arg in contract.optional.items():
        if name not in normalized:
            if arg.default is not None:
                output[name] = arg.default
            continue
        coerced = _coerce(
            tool,
            name,
            normalized[name],
            arg.type,
            required=False,
            singleton_dict=arg.singleton_dict,
            list_item_type=arg.list_item_type,
        )
        if not coerced.ok:
            return coerced
        output[name] = coerced.args[name]
    if tool == "knowledge_write" and str(output.get("type") or "").strip().lower() == "synthesis":
        return _invalid("done required for final synthesis; knowledge_write.type must not be 'synthesis'")
    return ContractResult(True, args=output)


def tool_example(tool: str) -> str:
    if tool == "web_search":
        return '{"tool":"web_search","args":{"query":"..."}}'
    if tool == "open_url":
        return '{"tool":"open_url","args":{"url":"https://...","offset":0,"limit":6000,"pages":""}}'
    if tool == "source_search":
        return '{"tool":"source_search","args":{"url":"https://...","query":"...","limit":6}}'
    if tool == "knowledge_search":
        return '{"tool":"knowledge_search","args":{"query":"..."}}'
    if tool == "knowledge_read":
        return '{"tool":"knowledge_read","args":{"id":"<note id>"}}'
    if tool == "knowledge_write":
        return (
            '{"tool":"knowledge_write","args":{"type":"fact","title":"...","body":"...",'
            '"sources":["https://..."],'
            '"relations":[{"src":"war","dst":"helium supply","kind":"affects"}]}}'
        )
    if tool == "knowledge_link":
        return '{"tool":"knowledge_link","args":{"src":"<note id>","dst":"<note id>","kind":"supports"}}'
    if tool == "done":
        return '{"tool":"done","args":{"answer":"<the full report>"}}'
    return '{"tool":"web_search","args":{"query":"..."}}'


def _apply_aliases(args: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    normalized = dict(args)
    for alias, target in aliases.items():
        if target in normalized:
            continue
        if alias in normalized:
            normalized[target] = normalized[alias]
    return normalized


def _coerce(
    tool: str,
    name: str,
    value: Any,
    expected: type,
    *,
    required: bool,
    singleton_dict: bool = False,
    list_item_type: type | None = None,
) -> ContractResult:
    if expected is str:
        text = _coerce_str(value)
        if required and not text:
            return _invalid(f"{tool}.{name} must be a non-empty string")
        return ContractResult(True, args={name: text})
    if expected is int:
        parsed = _coerce_int(value)
        if parsed is None:
            return _invalid(f"{tool}.{name} must be an integer")
        return ContractResult(True, args={name: parsed})
    if expected is float:
        parsed_float = _coerce_float(value)
        if parsed_float is None:
            return _invalid(f"{tool}.{name} must be a number")
        return ContractResult(True, args={name: parsed_float})
    if expected is list:
        parsed_list = _coerce_list(value, singleton_dict=singleton_dict)
        if parsed_list is None:
            return _invalid(f"{tool}.{name} must be a list")
        if list_item_type is not None and not all(
            isinstance(item, list_item_type) for item in parsed_list
        ):
            if list_item_type is dict:
                return _invalid(f"{tool}.{name} must be a list of objects")
            return _invalid(f"{tool}.{name} has invalid list item type")
        return ContractResult(True, args={name: parsed_list})
    return ContractResult(True, args={name: value})


def _coerce_str(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _coerce_str(item)
            if text:
                return text
        return ""
    if value is None or isinstance(value, (dict, set)):
        return ""
    return str(value).strip()


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _coerce_list(value: Any, *, singleton_dict: bool = False) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if singleton_dict and isinstance(value, dict):
        return [value]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return None


def _invalid(message: str) -> ContractResult:
    return ContractResult(False, error=message, error_kind=PROTOCOL_INVALID_ARGS)
