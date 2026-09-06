"""Structured shell approval request passed across the agent boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from codey.runtime.models import ToolCall


MAX_DEFERRED_TOOL_CALLS = 8
MAX_DEFERRED_TEXT_CHARS = 240


@dataclass(frozen=True)
class DeferredToolCall:
    tool_index: int
    tool_name: str
    path: str = ""
    command: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "tool_index": max(0, int(self.tool_index)),
            "tool_name": _bounded_text(self.tool_name, 80),
        }
        path = _bounded_text(self.path, MAX_DEFERRED_TEXT_CHARS)
        command = _bounded_text(self.command, MAX_DEFERRED_TEXT_CHARS)
        if path:
            payload["path"] = path
        if command:
            payload["command"] = command
        return payload


@dataclass(frozen=True)
class ShellApprovalRequest:
    cwd: str
    command: str
    deferred_calls: tuple[DeferredToolCall, ...] = ()

    def to_payload(self) -> dict[str, object]:
        deferred = tuple(self.deferred_calls[:MAX_DEFERRED_TOOL_CALLS])
        return {
            "cwd": _bounded_text(self.cwd or ".", MAX_DEFERRED_TEXT_CHARS),
            "command": _bounded_text(self.command, MAX_DEFERRED_TEXT_CHARS),
            "deferred_tool_count": len(self.deferred_calls),
            "deferred_tool_calls": [item.to_payload() for item in deferred],
        }


def deferred_tool_call_from_call(call: ToolCall, *, tool_index: int) -> DeferredToolCall:
    path = str(call.args.get("path") or "")
    command = str(call.args.get("command") or "") if call.name in {"run", "shell"} else ""
    return DeferredToolCall(
        tool_index=tool_index,
        tool_name=str(call.name or ""),
        path=path,
        command=command,
    )


def render_deferred_tool_calls(rows: Sequence[Mapping[str, object]]) -> str:
    items = [
        _render_deferred_tool_call(row)
        for row in rows[:MAX_DEFERRED_TOOL_CALLS]
        if isinstance(row, Mapping)
    ]
    if not items:
        return ""
    return (
        "Deferred tool calls from the paused model reply were not executed:\n"
        + "\n".join(f"- {item}" for item in items)
        + "\nReissue only the calls that are still correct after considering the shell result."
    )


def _render_deferred_tool_call(row: Mapping[str, object]) -> str:
    tool_index = _nonnegative_int(row.get("tool_index"))
    tool_name = _bounded_text(row.get("tool_name"), 80) or "unknown"
    path = _bounded_text(row.get("path"), MAX_DEFERRED_TEXT_CHARS)
    command = _bounded_text(row.get("command"), MAX_DEFERRED_TEXT_CHARS)
    parts = [f"#{tool_index}", tool_name]
    if path:
        parts.append(f"path={path}")
    if command:
        parts.append(f"command={command}")
    return " ".join(parts)


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


__all__ = [
    "DeferredToolCall",
    "ShellApprovalRequest",
    "deferred_tool_call_from_call",
    "render_deferred_tool_calls",
]
