"""Append-only runtime session log.

The log records bounded facts and references about runtime operations.  It does
not persist raw prompts, model replies, command output, or diffs.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key

SCHEMA_VERSION = 1
DEFAULT_MAX_ENTRY_BYTES = 64 * 1024
DEFAULT_MAX_LOG_BYTES = 4 * 1024 * 1024
RuntimeEntryKind = Literal[
    "operation_started",
    "operation_effect",
    "tool_invocation",
    "tool_settled",
    "operation_settled",
]

_ENTRY_KINDS = {
    "operation_started",
    "operation_effect",
    "tool_invocation",
    "tool_settled",
    "operation_settled",
}
_FORBIDDEN_PAYLOAD_KEYS = {
    "diff",
    "prompt",
    "raw_diff",
    "raw_prompt",
    "raw_reply",
    "raw_stderr",
    "raw_stdout",
    "reply",
    "stderr",
    "stdout",
}


class RuntimeLogError(Exception):
    """Base class for runtime log failures."""


class RuntimeLogCorruption(RuntimeLogError):
    """The log violates the runtime replay contract."""


class RuntimeLogWriteError(RuntimeLogError):
    """The log cannot accept a new entry without violating a guard."""


@dataclass(frozen=True)
class RuntimeLogEntry:
    session_id: str
    lane: str
    operation_id: str
    kind: RuntimeEntryKind | str
    payload: dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""
    created_at: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeLogCorruption("unsupported runtime log schema")
        for field_name in ("session_id", "lane", "operation_id", "kind"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeLogCorruption(f"{field_name} must be a non-empty string")
        if self.kind not in _ENTRY_KINDS:
            raise RuntimeLogCorruption(f"unknown runtime log entry kind: {self.kind}")
        if not isinstance(self.payload, dict):
            raise RuntimeLogCorruption("payload must be an object")
        offender = _forbidden_payload_key(self.payload)
        if offender:
            raise RuntimeLogWriteError(f"runtime log payload contains raw field: {offender}")
        if not self.entry_id:
            object.__setattr__(self, "entry_id", f"entry-{uuid.uuid4().hex}")
        if not self.created_at:
            object.__setattr__(self, "created_at", time.time())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "lane": self.lane,
            "operation_id": self.operation_id,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    def to_json_line(self) -> str:
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"

    @classmethod
    def from_payload(cls, payload: object) -> "RuntimeLogEntry":
        if not isinstance(payload, dict):
            raise RuntimeLogCorruption("runtime log entry must be an object")
        return cls(
            schema_version=payload.get("schema_version"),
            entry_id=payload.get("entry_id") or "",
            created_at=float(payload.get("created_at") or 0.0),
            session_id=payload.get("session_id"),
            lane=payload.get("lane"),
            operation_id=payload.get("operation_id"),
            kind=payload.get("kind"),
            payload=payload.get("payload") or {},
        )


def _forbidden_payload_key(value: object) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _FORBIDDEN_PAYLOAD_KEYS:
                return lowered
            nested = _forbidden_payload_key(item)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _forbidden_payload_key(item)
            if nested:
                return nested
    return ""


class RuntimeSessionLog:
    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        max_entry_bytes: int = DEFAULT_MAX_ENTRY_BYTES,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    ) -> None:
        self.state_home = Path(state_home) if state_home is not None else DEFAULT_STATE_HOME
        self.max_entry_bytes = max_entry_bytes
        self.max_log_bytes = max_log_bytes

    def path_for(self, session_id: str) -> Path:
        return self.state_home / "runtime" / "sessions" / f"{session_key(session_id)}.jsonl"

    def read(self, session_id: str) -> tuple[RuntimeLogEntry, ...]:
        path = self.path_for(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RuntimeLogCorruption("unable to read runtime log") from exc
        entries: list[RuntimeLogEntry] = []
        for line_no, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeLogCorruption(f"invalid runtime log JSON at line {line_no}") from exc
            entry = RuntimeLogEntry.from_payload(payload)
            if entry.session_id != session_id:
                raise RuntimeLogCorruption("runtime log entry session mismatch")
            entries.append(entry)
        return tuple(entries)

    def append(
        self,
        session_id: str,
        *,
        lane: str,
        operation_id: str,
        kind: RuntimeEntryKind | str,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeLogEntry:
        entry = RuntimeLogEntry(
            session_id=session_id,
            lane=lane,
            operation_id=operation_id,
            kind=kind,
            payload=dict(payload or {}),
        )
        encoded = entry.to_json_line().encode("utf-8")
        if len(encoded) > self.max_entry_bytes:
            raise RuntimeLogWriteError("runtime log entry exceeds size limit")
        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with with_file_lock(path):
            entries = self.read(session_id)
            from codey.runtime.reducer import reduce_session

            reduce_session((*entries, entry))
            current_size = path.stat().st_size if path.exists() else 0
            if current_size + len(encoded) > self.max_log_bytes:
                raise RuntimeLogWriteError("runtime log exceeds size limit")
            with path.open("ab") as handle:
                handle.write(encoded)
        return entry
