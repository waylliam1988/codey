"""Structured shell approval request passed across the agent boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from codey.runtime.core.models import ToolCall

MAX_DEFERRED_TOOL_CALLS = 8
MAX_DEFERRED_TEXT_CHARS = 240
MAX_APPROVAL_COMMAND_CHARS = 1_000
TRUNCATED_COMMAND_MARKER = "\n[truncated; command_sha256={digest}]"


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
            **shell_command_payload(self.command),
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


def shell_command_payload(
    command: object,
    *,
    limit: int = MAX_APPROVAL_COMMAND_CHARS,
) -> dict[str, object]:
    """Bounded approval-card command plus full-text identity metadata."""

    full = shell_command_text(command)
    digest = _sha256_text(full)
    truncated = len(full) > max(0, int(limit))
    return {
        "command": _bounded_command_text(full, limit, digest=digest),
        "command_sha256": digest,
        "command_chars": len(full),
        "command_truncated": truncated,
    }


def shell_command_event_fields(
    record: Mapping[str, object],
    *,
    limit: int = MAX_APPROVAL_COMMAND_CHARS,
) -> dict[str, object]:
    """Return event-safe command fields from a pending approval record.

    New pending records carry ``command_preview`` from the original full
    command. Legacy/test records may only carry ``command``; bound them here
    so any event path remains safe by default.
    """

    digest = str(record.get("command_sha256") or "").strip().lower()
    has_digest = _is_sha256_hex(digest)
    preview = str(record.get("command_preview") or "")
    if not preview and has_digest:
        preview = _bounded_command_text(
            str(record.get("command") or ""),
            limit,
            digest=digest,
        )
    if preview:
        payload: dict[str, object] = {"command": preview}
        if has_digest:
            payload["command_sha256"] = digest
        chars = _nonnegative_int(record.get("command_chars"))
        if chars:
            payload["command_chars"] = chars
        if "command_truncated" in record:
            payload["command_truncated"] = bool(record.get("command_truncated"))
        else:
            payload["command_truncated"] = len(str(record.get("command") or "")) > limit
        return payload
    return shell_command_payload(record.get("command"), limit=limit)


def shell_command_text(command: object) -> str:
    return str(command or "").strip()


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _bounded_command_text(text: str, limit: int, *, digest: str) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = TRUNCATED_COMMAND_MARKER.format(digest=digest)
    if len(marker) >= limit:
        return marker[:limit]
    return text[: limit - len(marker)].rstrip() + marker


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


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
    "MAX_APPROVAL_COMMAND_CHARS",
    "ShellApprovalRequest",
    "deferred_tool_call_from_call",
    "render_deferred_tool_calls",
    "shell_command_event_fields",
    "shell_command_payload",
    "shell_command_text",
]
