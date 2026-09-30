"""Structured shell approval request passed across the agent boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from codey.runtime.core.models import ToolCall

MAX_DEFERRED_TOOL_CALLS = 8
MAX_DEFERRED_TEXT_CHARS = 240
MAX_APPROVAL_COMMAND_CHARS = 1_000
# 原生协议调用身份原样保留：与持久化 effect 记录同一上界，永不截断。
MAX_SHELL_CALL_ID_CHARS = 256
TRUNCATED_COMMAND_MARKER = "\n[truncated; command_sha256={digest}]"


def valid_shell_call_id(value: object) -> bool:
    """True when a shell call id may be used verbatim (never truncated)."""
    if not isinstance(value, str):
        return False
    return bool(value.strip()) and len(value) <= MAX_SHELL_CALL_ID_CHARS


@dataclass(frozen=True)
class DeferredToolCall:
    tool_index: int
    tool_name: str
    path: str = ""
    command: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "tool_index": _nonnegative_int(self.tool_index),
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
    call_id: str = ""
    provider_id: str = ""
    turn: int = 0
    tool_index: int = 0

    def to_payload(self) -> dict[str, object]:
        deferred = tuple(self.deferred_calls[:MAX_DEFERRED_TOOL_CALLS])
        payload: dict[str, object] = {
            "cwd": _bounded_text(self.cwd or ".", MAX_DEFERRED_TEXT_CHARS),
            **shell_command_payload(self.command),
            "deferred_tool_count": len(self.deferred_calls),
            "deferred_tool_calls": [item.to_payload() for item in deferred],
        }
        # 原生调用身份原样保留：合法才带，不合法或超限直接省略，
        # 绝不截断成另一个 id。
        if valid_shell_call_id(self.call_id):
            payload["call_id"] = self.call_id
        elif str(self.call_id or "").strip():
            payload["call_id_invalid"] = True
        if str(self.provider_id or "").strip():
            payload["provider_id"] = _bounded_text(self.provider_id, MAX_DEFERRED_TEXT_CHARS)
        if type(self.turn) is int and self.turn >= 0:
            payload["turn"] = self.turn
        if type(self.tool_index) is int and self.tool_index >= 0:
            payload["tool_index"] = self.tool_index
        return payload


def deferred_tool_call_from_call(call: ToolCall, *, tool_index: int) -> DeferredToolCall:
    path = str(call.args.get("path") or "")
    command = str(call.args.get("command") or "") if call.name in {"run", "shell"} else ""
    return DeferredToolCall(
        tool_index=tool_index,
        tool_name=str(call.name or ""),
        path=path,
        command=command,
    )


def build_shell_approval_pending(
    *,
    approval: ShellApprovalRequest,
    approval_id: str,
    session_id: str,
    run_id: str,
    project: str,
    max_turns: int,
    provider_label: str,
    command_fields: dict[str, object] | None = None,
    risk_label: str = "generic",
    risk_title: str = "",
    risk_detail: str = "",
    post_approval_instructions: str = "",
) -> dict[str, object]:
    """Build the persisted approval record; the single owner of its shape.

    原生调用身份（call id、provider 会话、turn/tool_index）完整持久化，
    批准/拒绝/取消后凭此回答原调用。call id 合法才原样保存，否则省略
    并标记 invalid（绝不截断使用）。
    """
    cwd_rel = approval.cwd or "."
    fields = dict(command_fields) if isinstance(command_fields, dict) else dict(
        shell_command_payload(approval.command)
    )
    deferred_tool_calls = [item.to_payload() for item in approval.deferred_calls]
    pending: dict[str, object] = {
        "id": approval_id,
        "session_id": session_id,
        "project": project,
        "cwd": cwd_rel or ".",
        "command": shell_command_text(approval.command),
        "command_preview": fields.get("command"),
        "command_sha256": fields.get("command_sha256"),
        "command_chars": fields.get("command_chars"),
        "command_truncated": fields.get("command_truncated"),
        "risk_label": risk_label,
        "risk_title": risk_title,
        "risk_detail": risk_detail,
        "post_approval_instructions": post_approval_instructions,
        "max_turns": max_turns,
        "provider": provider_label,
        "continue_after": True,
        "run_id": run_id,
        "deferred_tool_count": len(approval.deferred_calls),
        "deferred_tool_calls": deferred_tool_calls,
    }
    if valid_shell_call_id(approval.call_id):
        pending["call_id"] = approval.call_id
    elif str(approval.call_id or "").strip():
        pending["call_id_invalid"] = True
    if str(approval.provider_id or "").strip():
        pending["provider_id"] = str(approval.provider_id).strip()
    if type(approval.turn) is int and approval.turn >= 0:
        pending["turn"] = approval.turn
    if type(approval.tool_index) is int and approval.tool_index >= 0:
        pending["tool_index"] = approval.tool_index
    pending["ui_event"] = {
        "type": "shell_request",
        "run_id": run_id,
        "session_id": session_id,
        "id": approval_id,
        "project": project,
        "cwd": pending["cwd"],
        **{k: v for k, v in fields.items() if k in {
            "command", "command_sha256", "command_chars", "command_truncated"}},
        "risk_label": risk_label,
        "risk_title": risk_title,
        "risk_detail": risk_detail,
        "deferred_tool_count": len(approval.deferred_calls),
        "deferred_tool_calls": deferred_tool_calls,
    }
    if "call_id" in pending:
        pending["ui_event"]["call_id"] = pending["call_id"]
    return pending


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
    safe_limit = _nonnegative_int(limit)
    truncated = len(full) > safe_limit
    return {
        "command": _bounded_command_text(full, safe_limit, digest=digest),
        "command_sha256": digest,
        "command_chars": len(full),
        "command_truncated": truncated,
    }


def shell_command_event_fields(
    record: Mapping[str, object],
    *,
    limit: int = MAX_APPROVAL_COMMAND_CHARS,
) -> dict[str, object]:
    """Return event-safe command fields from a current approval record.

    Cold-start contract: legacy/test records carrying only ``command`` are
    rejected. Two current shapes are accepted:

    - pending record: full ``command`` plus ``command_preview``,
      ``command_sha256``, ``command_chars``, ``command_truncated``. The
      preview/sha/chars/flag are recomputed from the full text and must
      match exactly (truncation bound + hash are the security boundary).
    - event record: bounded ``command`` (already a preview) plus
      ``command_sha256``, ``command_chars``, ``command_truncated``. When
      not truncated the digest is recomputed from the command, so plain
      marker-like text stays legal; when truncated the exact trailing
      marker is required (full-command hash binding stays in the pending
      path that generated the event).
    """

    if not isinstance(record, Mapping):
        raise TypeError("approval record must be a mapping")
    effective_limit = _nonnegative_int(limit)
    if "command_preview" in record:
        return _pending_event_fields(record, limit=effective_limit)
    if (
        "command_sha256" in record
        and "command_chars" in record
        and "command_truncated" in record
    ):
        return _event_record_fields(record, limit=effective_limit)
    raise ValueError("approval record is missing current command fields")


def _pending_event_fields(
    record: Mapping[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    for key in ("command", "command_preview", "command_sha256", "command_chars", "command_truncated"):
        if key not in record:
            raise ValueError(f"approval record is missing {key}")
    expected = shell_command_payload(record.get("command"), limit=limit)
    preview = record.get("command_preview")
    digest = record.get("command_sha256")
    chars = record.get("command_chars")
    truncated = record.get("command_truncated")
    if not isinstance(preview, str):
        raise ValueError("command_preview must be a string")
    if not isinstance(digest, str):
        raise ValueError("command_sha256 must be a string")
    if type(chars) is not int:
        raise ValueError("command_chars must be an int")
    if type(truncated) is not bool:
        raise ValueError("command_truncated must be a bool")
    if digest != digest.lower() or not _is_sha256_hex(digest):
        raise ValueError("command_sha256 must be lowercase hex")
    if (
        preview != expected["command"]
        or digest != expected["command_sha256"]
        or chars != expected["command_chars"]
        or truncated != expected["command_truncated"]
    ):
        raise ValueError("approval record command fields do not match full command")
    return {
        "command": expected["command"],
        "command_sha256": expected["command_sha256"],
        "command_chars": expected["command_chars"],
        "command_truncated": expected["command_truncated"],
    }


def _event_record_fields(
    record: Mapping[str, object],
    *,
    limit: int,
) -> dict[str, object]:
    command = record.get("command")
    digest = record.get("command_sha256")
    chars = record.get("command_chars")
    truncated = record.get("command_truncated")
    if not isinstance(command, str):
        raise ValueError("command must be a string")
    if not isinstance(digest, str):
        raise ValueError("command_sha256 must be a string")
    if type(chars) is not int:
        raise ValueError("command_chars must be an int")
    if type(truncated) is not bool:
        raise ValueError("command_truncated must be a bool")
    if chars < 0:
        raise ValueError("command_chars must be non-negative")
    if digest != digest.lower() or not _is_sha256_hex(digest):
        raise ValueError("command_sha256 must be lowercase hex")
    if len(command) > limit:
        raise ValueError("event command exceeds display bound")
    if truncated:
        if chars <= limit or chars <= len(command):
            raise ValueError("truncated event must carry full length above bound")
        exact_marker = TRUNCATED_COMMAND_MARKER.format(digest=digest)
        if limit < len(exact_marker):
            if command != exact_marker[:limit]:
                raise ValueError("truncated event must carry exact marker prefix")
        elif not command.endswith(exact_marker):
            raise ValueError("truncated event must carry exact trailing marker")
    else:
        if chars != len(command):
            raise ValueError("event command_chars must match preview length")
        if digest != _sha256_text(command):
            raise ValueError("event command_sha256 must match command")
    return {
        "command": command,
        "command_sha256": digest,
        "command_chars": chars,
        "command_truncated": truncated,
    }


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
    if isinstance(value, str):
        text = value.strip()
        if text.isascii() and text.isdigit():
            try:
                return int(text)
            except ValueError:
                return 0
    return 0


__all__ = [
    "DeferredToolCall",
    "MAX_APPROVAL_COMMAND_CHARS",
    "MAX_SHELL_CALL_ID_CHARS",
    "ShellApprovalRequest",
    "build_shell_approval_pending",
    "deferred_tool_call_from_call",
    "render_deferred_tool_calls",
    "shell_command_event_fields",
    "shell_command_payload",
    "shell_command_text",
    "valid_shell_call_id",
]
