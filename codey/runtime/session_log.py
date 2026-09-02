"""Append-only runtime session log.

The log records bounded facts and references about runtime operations.  It does
not persist raw prompts, model replies, command output, or diffs.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from codey.storage.atomic_io import write_bytes_atomic
from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import DEFAULT_STATE_HOME, session_key

SCHEMA_VERSION = 1
DEFAULT_MAX_ENTRY_BYTES = 64 * 1024
DEFAULT_MAX_LOG_BYTES = 4 * 1024 * 1024
RuntimeEntryKind = Literal[
    "operation_started",
    "operation_effect",
    "operation_settled",
]

_ENTRY_KINDS = {
    "operation_started",
    "operation_effect",
    "operation_settled",
}
_KNOWN_ENTRY_KEYS = frozenset(
    {
        "schema_version",
        "entry_id",
        "created_at",
        "session_id",
        "lane",
        "operation_id",
        "kind",
        "payload",
        "batch_id",
        "batch_index",
        "batch_count",
    }
)
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
class _FileStamp:
    file_size: int
    mtime_ns: int


@dataclass(frozen=True)
class _ProjectionCache:
    stamp: _FileStamp
    entries: tuple["RuntimeLogEntry", ...]
    projection: Any


@dataclass(frozen=True)
class RuntimeLogEntry:
    session_id: str
    lane: str
    operation_id: str
    kind: RuntimeEntryKind | str
    payload: dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""
    created_at: float = 0.0
    batch_id: str = ""
    batch_index: int = 0
    batch_count: int = 1
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeLogCorruption("unsupported runtime log schema")
        for field_name in ("session_id", "lane", "operation_id", "kind"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value.strip() != value:
                raise RuntimeLogCorruption(f"{field_name} must be a non-empty string")
        if self.kind not in _ENTRY_KINDS:
            raise RuntimeLogCorruption(f"unknown runtime log entry kind: {self.kind}")
        if not isinstance(self.payload, dict):
            raise RuntimeLogCorruption("payload must be an object")
        offender = _forbidden_payload_key(self.payload)
        if offender:
            raise RuntimeLogWriteError(f"runtime log payload contains raw field: {offender}")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)):
            raise RuntimeLogCorruption("created_at must be a finite number")
        created_at = float(self.created_at)
        if not math.isfinite(created_at) or created_at < 0:
            raise RuntimeLogCorruption("created_at must be a finite number")
        if not self.entry_id:
            object.__setattr__(self, "entry_id", f"entry-{uuid.uuid4().hex}")
        elif not isinstance(self.entry_id, str) or not self.entry_id.strip() or self.entry_id.strip() != self.entry_id:
            raise RuntimeLogCorruption("entry_id must be a non-empty string")
        if not created_at:
            object.__setattr__(self, "created_at", time.time())
        else:
            object.__setattr__(self, "created_at", created_at)
        if not self.batch_id:
            object.__setattr__(self, "batch_id", f"batch-{uuid.uuid4().hex}")
        elif not isinstance(self.batch_id, str) or not self.batch_id.strip() or self.batch_id.strip() != self.batch_id:
            raise RuntimeLogCorruption("batch_id must be a non-empty string")
        if (
            isinstance(self.batch_index, bool)
            or not isinstance(self.batch_index, int)
            or self.batch_index < 0
        ):
            raise RuntimeLogCorruption("batch_index must be a non-negative integer")
        if (
            isinstance(self.batch_count, bool)
            or not isinstance(self.batch_count, int)
            or self.batch_count < 1
        ):
            raise RuntimeLogCorruption("batch_count must be a positive integer")
        if self.batch_index >= self.batch_count:
            raise RuntimeLogCorruption("batch_index must be inside the batch")

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
            "batch_id": self.batch_id,
            "batch_index": self.batch_index,
            "batch_count": self.batch_count,
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
        if set(payload) - _KNOWN_ENTRY_KEYS:
            raise RuntimeLogCorruption("runtime log entry carries unknown keys")
        entry_id = payload.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip() or entry_id.strip() != entry_id:
            raise RuntimeLogCorruption("entry_id must be a non-empty string")
        created_at = payload.get("created_at")
        if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
            raise RuntimeLogCorruption("created_at must be a finite number")
        created_at = float(created_at)
        if not math.isfinite(created_at) or created_at <= 0:
            raise RuntimeLogCorruption("created_at must be a positive finite number")
        entry_payload = payload.get("payload")
        if not isinstance(entry_payload, dict):
            raise RuntimeLogCorruption("payload must be an object")
        batch_id = payload.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip() or batch_id.strip() != batch_id:
            raise RuntimeLogCorruption("batch_id must be a non-empty string")
        batch_index = payload.get("batch_index")
        if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0:
            raise RuntimeLogCorruption("batch_index must be a non-negative integer")
        batch_count = payload.get("batch_count")
        if isinstance(batch_count, bool) or not isinstance(batch_count, int) or batch_count < 1:
            raise RuntimeLogCorruption("batch_count must be a positive integer")
        if batch_index >= batch_count:
            raise RuntimeLogCorruption("batch_index must be inside the batch")
        return cls(
            schema_version=payload.get("schema_version"),
            entry_id=entry_id,
            created_at=created_at,
            session_id=payload.get("session_id"),
            lane=payload.get("lane"),
            operation_id=payload.get("operation_id"),
            kind=payload.get("kind"),
            payload=entry_payload,
            batch_id=batch_id,
            batch_index=batch_index,
            batch_count=batch_count,
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
        self._projection_cache: dict[str, _ProjectionCache] = {}

    def path_for(self, session_id: str) -> Path:
        return self.state_home / "runtime" / "sessions" / f"{session_key(session_id)}.jsonl"

    def read(self, session_id: str) -> tuple[RuntimeLogEntry, ...]:
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._read_unlocked(session_id, repair_tail=False)

    def projection(self, session_id: str):
        """Return the cached projection for a session.

        The returned projection is the process-cache object; callers must treat
        it as read-only and derive new state through reducer.apply_entries().
        """
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._cache_for_session_unlocked(session_id).projection

    def entries(self, session_id: str) -> tuple[RuntimeLogEntry, ...]:
        """Return cached valid entries, repairing a torn tail before caching."""
        path = self.path_for(session_id)
        with with_file_lock(path):
            return self._cache_for_session_unlocked(session_id).entries

    def _cache_for_session_unlocked(self, session_id: str) -> _ProjectionCache:
        path = self.path_for(session_id)
        current_stamp = _file_stamp(path)
        cached = self._projection_cache.get(session_id)
        if cached is not None and cached.stamp == current_stamp:
            return cached

        entries = self._read_unlocked(session_id, repair_tail=True)
        from codey.runtime.reducer import reduce_session

        cache = _ProjectionCache(
            stamp=_file_stamp(path),
            entries=entries,
            projection=reduce_session(entries),
        )
        self._projection_cache[session_id] = cache
        return cache

    def delete_session(self, session_id: str) -> None:
        path = self.path_for(session_id)
        with with_file_lock(path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RuntimeLogWriteError("unable to delete runtime log") from exc
            finally:
                self._projection_cache.pop(session_id, None)

    def _read_unlocked(
        self,
        session_id: str,
        *,
        repair_tail: bool,
    ) -> tuple[RuntimeLogEntry, ...]:
        path = self.path_for(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RuntimeLogCorruption("unable to read runtime log") from exc
        lines = [
            (line_no, line)
            for line_no, line in enumerate(raw.splitlines(), start=1)
            if line.strip()
        ]
        entries: list[RuntimeLogEntry] = []
        bad_tail = False
        for index, (line_no, line) in enumerate(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    bad_tail = True
                    break
                raise RuntimeLogCorruption(f"invalid runtime log JSON at line {line_no}") from exc
            entry = RuntimeLogEntry.from_payload(payload)
            if entry.session_id != session_id:
                raise RuntimeLogCorruption("runtime log entry session mismatch")
            entries.append(entry)
        valid = _complete_batch_prefix(entries)
        if repair_tail and (bad_tail or len(valid) < len(entries)):
            write_bytes_atomic(path, b"".join(entry.to_json_line().encode("utf-8") for entry in valid))
            self._projection_cache.pop(session_id, None)
        return tuple(valid)

    def append(
        self,
        session_id: str,
        *,
        lane: str,
        operation_id: str,
        kind: RuntimeEntryKind | str,
        payload: dict[str, Any] | None = None,
    ) -> RuntimeLogEntry:
        return self.append_many(
            session_id,
            (
                {
                    "lane": lane,
                    "operation_id": operation_id,
                    "kind": kind,
                    "payload": {} if payload is None else payload,
                },
            ),
        )[0]

    def append_many(
        self,
        session_id: str,
        entries: Iterable[dict[str, Any]],
    ) -> tuple[RuntimeLogEntry, ...]:
        incoming = tuple(entries)
        if not incoming:
            return ()
        batch_id = f"batch-{uuid.uuid4().hex}"
        batch_count = len(incoming)
        rows = tuple(
            RuntimeLogEntry(
                session_id=session_id,
                lane=entry.get("lane"),
                operation_id=entry.get("operation_id"),
                kind=entry.get("kind"),
                payload=entry.get("payload"),
                batch_id=batch_id,
                batch_index=index,
                batch_count=batch_count,
            )
            for index, entry in enumerate(incoming)
        )
        encoded_rows = tuple(row.to_json_line().encode("utf-8") for row in rows)
        if any(len(encoded) > self.max_entry_bytes for encoded in encoded_rows):
            raise RuntimeLogWriteError("runtime log entry exceeds size limit")
        path = self.path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with with_file_lock(path):
            from codey.runtime.reducer import apply_entries, reduce_session

            current_stamp = _file_stamp(path)
            current_size = current_stamp.file_size
            cached = self._projection_cache.get(session_id)
            if cached is not None and cached.stamp == current_stamp:
                base_entries = cached.entries
                base_projection = cached.projection
            else:
                base_entries = self._read_unlocked(session_id, repair_tail=True)
                current_stamp = _file_stamp(path)
                current_size = current_stamp.file_size

                base_projection = reduce_session(base_entries)

            candidate_projection = apply_entries(base_projection, rows)
            total_new_bytes = sum(len(encoded) for encoded in encoded_rows)
            if current_size + total_new_bytes > max(0, self.max_log_bytes // 2):
                candidate = (*base_entries, *rows)
                compacted = _compact_entries(candidate)
                compacted_projection = reduce_session(compacted)
                encoded_compacted = tuple(
                    row.to_json_line().encode("utf-8") for row in compacted
                )
                if any(len(encoded) > self.max_entry_bytes for encoded in encoded_compacted):
                    raise RuntimeLogWriteError("runtime log entry exceeds size limit")
                if sum(len(encoded) for encoded in encoded_compacted) > self.max_log_bytes:
                    raise RuntimeLogWriteError("runtime log exceeds size limit")
                write_bytes_atomic(path, b"".join(encoded_compacted))
                self._projection_cache[session_id] = _ProjectionCache(
                    stamp=_file_stamp(path),
                    entries=compacted,
                    projection=compacted_projection,
                )
                return rows
            with path.open("ab") as handle:
                for encoded in encoded_rows:
                    handle.write(encoded)
            self._projection_cache[session_id] = _ProjectionCache(
                stamp=_file_stamp(path),
                entries=(*base_entries, *rows),
                projection=candidate_projection,
            )
        return rows


def _file_stamp(path: Path) -> _FileStamp:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _FileStamp(file_size=0, mtime_ns=0)
    return _FileStamp(file_size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _nonnegative_payload_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _complete_batch_prefix(
    entries: list[RuntimeLogEntry],
) -> list[RuntimeLogEntry]:
    valid: list[RuntimeLogEntry] = []
    index = 0
    while index < len(entries):
        first = entries[index]
        batch_id = first.batch_id
        batch_count = first.batch_count
        batch: list[RuntimeLogEntry] = []
        for offset in range(batch_count):
            row_index = index + offset
            if row_index >= len(entries):
                return valid
            entry = entries[row_index]
            if (
                entry.batch_id != batch_id
                or entry.batch_count != batch_count
                or entry.batch_index != offset
            ):
                if row_index >= len(entries) - 1:
                    return valid
                raise RuntimeLogCorruption("runtime log batch is not contiguous")
            batch.append(entry)
        valid.extend(batch)
        index += batch_count
    return valid


def _compact_entries(
    entries: tuple[RuntimeLogEntry, ...],
) -> tuple[RuntimeLogEntry, ...]:
    """Keep the replay-equivalent task-operation spine and recovery facts."""
    ordered_operations: list[str] = []
    started: dict[str, RuntimeLogEntry] = {}
    latest_phase: dict[str, RuntimeLogEntry] = {}
    settled: dict[str, RuntimeLogEntry] = {}
    operation_effects: dict[str, list[RuntimeLogEntry]] = {}
    delivery_effects: dict[str, list[RuntimeLogEntry]] = {}

    for entry in entries:
        if entry.kind == "operation_started":
            if entry.operation_id not in started:
                ordered_operations.append(entry.operation_id)
            started[entry.operation_id] = entry
            continue
        if entry.kind == "operation_effect":
            effect_kind = entry.payload.get("effect_kind")
            if effect_kind == "run_phase":
                latest_phase[entry.operation_id] = entry
            elif effect_kind == "runtime_effect":
                operation_effects.setdefault(entry.operation_id, []).append(entry)
            elif effect_kind == "tool_result_delivery":
                delivery_effects.setdefault(entry.operation_id, []).append(entry)
            continue
        if entry.kind == "operation_settled":
            settled[entry.operation_id] = entry
    compacted: list[RuntimeLogEntry] = []
    for operation_id in ordered_operations:
        start = started.get(operation_id)
        if start is None:
            continue
        compacted.append(start)
        phase = latest_phase.get(operation_id)
        if phase is not None:
            compacted.append(phase)

        is_open = operation_id not in settled
        raw_effects = operation_effects.get(operation_id, [])
        intents: dict[str, RuntimeLogEntry] = {}
        settlements: dict[str, RuntimeLogEntry] = {}
        ordered_effect_ids: list[str] = []

        for eff in raw_effects:
            eid = str(eff.payload.get("effect_id") or "")
            rkind = eff.payload.get("record_kind")
            if not eid:
                continue
            if rkind == "intent":
                if eid not in intents:
                    ordered_effect_ids.append(eid)
                intents[eid] = eff
            elif rkind == "settlement":
                settlements[eid] = eff

        for eid in ordered_effect_ids:
            intent_entry = intents.get(eid)
            settlement_entry = settlements.get(eid)
            if intent_entry is None:
                continue
            if is_open:
                compacted.append(intent_entry)
                if settlement_entry is not None:
                    compacted.append(settlement_entry)
            else:
                if settlement_entry is not None:
                    status = settlement_entry.payload.get("status")
                    sent_state = settlement_entry.payload.get("sent_state")
                    replay_count = _nonnegative_payload_int(settlement_entry.payload.get("replay_count"))
                    if status in {"interrupted", "error"} or sent_state == "maybe_sent" or replay_count > 0:
                        compacted.append(intent_entry)
                        compacted.append(settlement_entry)

        raw_deliveries = delivery_effects.get(operation_id, [])
        for deliv in raw_deliveries:
            rkind = deliv.payload.get("record_kind")
            bid = str(deliv.payload.get("batch_id") or "")
            if not bid or not rkind:
                continue
            if is_open:
                compacted.append(deliv)
            else:
                if rkind == "recovered":
                    compacted.append(deliv)

        finish = settled.get(operation_id)
        if finish is not None:
            compacted.append(finish)
    return _rebatch(compacted)


def _rebatch(entries: list[RuntimeLogEntry]) -> tuple[RuntimeLogEntry, ...]:
    if not entries:
        return ()
    batch_id = f"batch-{uuid.uuid4().hex}"
    batch_count = len(entries)
    return tuple(
        RuntimeLogEntry(
            session_id=entry.session_id,
            lane=entry.lane,
            operation_id=entry.operation_id,
            kind=entry.kind,
            payload=dict(entry.payload),
            entry_id=entry.entry_id,
            created_at=entry.created_at,
            batch_id=batch_id,
            batch_index=index,
            batch_count=batch_count,
        )
        for index, entry in enumerate(entries)
    )
