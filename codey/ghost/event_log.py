"""Shared JSONL event log primitives for Ghost stores."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid
from typing import Callable, Iterable, Literal

from codey.storage.file_lock import with_file_lock
from codey.storage.local_store import delete_file


BadRowPolicy = Literal["warn", "block", "quarantine_tail"]


@dataclass(frozen=True)
class GhostEventRead:
    rows: tuple[dict[str, object], ...]
    warnings: tuple[str, ...] = ()
    blocked: bool = False


class GhostEventLog:
    """Small append-only JSONL helper with explicit read diagnostics."""

    def __init__(
        self,
        path: str | Path,
        *,
        schema_version: int,
        max_bytes: int | None = None,
        max_warnings: int = 20,
        source_name: str = "",
        allowed_event_kinds: Iterable[str] | None = None,
        bad_row_policy: BadRowPolicy = "block",
        event_validator: Callable[[dict[str, object]], bool] | None = None,
    ) -> None:
        self.path = Path(path)
        self.schema_version = int(schema_version)
        self.max_bytes = max_bytes
        self.max_warnings = max(0, int(max_warnings))
        self.source_name = source_name or self.path.name
        self.allowed_event_kinds = frozenset(
            str(item or "").strip()
            for item in (allowed_event_kinds or ())
            if str(item or "").strip()
        )
        if bad_row_policy not in {"warn", "block", "quarantine_tail"}:
            raise ValueError("bad_row_policy must be warn, block, or quarantine_tail")
        self.bad_row_policy = bad_row_policy
        self.event_validator = event_validator

    def read(self) -> GhostEventRead:
        with with_file_lock(self.path):
            return self.read_locked()

    def read_tail(self, max_rows: int) -> GhostEventRead:
        """Read and validate the last JSONL rows without parsing the full log."""
        count = max(0, int(max_rows or 0))
        if count == 0:
            return GhostEventRead(())
        with with_file_lock(self.path):
            try:
                if not self.path.is_file():
                    return GhostEventRead(())
                event_bytes = self.path.stat().st_size
                if self.max_bytes is not None and event_bytes > self.max_bytes:
                    return GhostEventRead((), (f"{self.source_name}:too_large",), True)
                line_count = count_jsonl_rows(self.path)
                lines = _tail_lines(self.path, count, event_bytes)
            except (OSError, UnicodeDecodeError):
                return GhostEventRead((), (f"{self.source_name}:unreadable",), True)

        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        first_line = max(1, line_count - len(lines) + 1)
        for offset, line in enumerate(lines):
            if not line.strip():
                continue
            payload, warning = self._decode_line(line, first_line + offset)
            if warning:
                warnings.append(warning)
                if self.bad_row_policy != "warn":
                    return GhostEventRead((), tuple(warnings[: self.max_warnings]), True)
                continue
            assert payload is not None
            rows.append(payload)
        return GhostEventRead(tuple(rows), tuple(warnings[: self.max_warnings]))

    def read_locked(self) -> GhostEventRead:
        """Read events when the caller already holds this log's file lock."""
        try:
            if not self.path.is_file():
                return GhostEventRead(())
            if self.max_bytes is not None and self.path.stat().st_size > self.max_bytes:
                return GhostEventRead((), (f"{self.source_name}:too_large",), True)
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return GhostEventRead((), (f"{self.source_name}:unreadable",), True)

        rows: list[dict[str, object]] = []
        warnings: list[str] = []
        for index, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            payload, warning = self._decode_line(line, index)
            if warning:
                warnings.append(warning)
                if self.bad_row_policy == "block":
                    return GhostEventRead((), tuple(warnings[: self.max_warnings]), True)
                if self.bad_row_policy == "quarantine_tail":
                    return self._quarantine_tail(
                        lines,
                        rows,
                        warnings,
                        index,
                    )
                continue
            assert payload is not None
            rows.append(payload)
        return GhostEventRead(tuple(rows), tuple(warnings[: self.max_warnings]))

    def append(self, events: Iterable[dict[str, object]]) -> bool:
        rows = list(events)
        if not rows:
            return True
        try:
            for event in rows:
                warning = self._validate_event(event, 0)
                if warning:
                    return False
            encoded = [json_line(event) for event in rows]
            encoded_bytes = [line.encode("utf-8") for line in encoded]
            new_bytes = sum(len(item) for item in encoded_bytes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with with_file_lock(self.path):
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if self.max_bytes is not None and current_size + new_bytes > self.max_bytes:
                    return False
                with self.path.open("ab") as handle:
                    for line in encoded_bytes:
                        handle.write(line)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def write_atomic(self, events: Iterable[dict[str, object]]) -> None:
        with with_file_lock(self.path):
            self.write_atomic_locked(events)

    def write_atomic_locked(self, events: Iterable[dict[str, object]]) -> None:
        """Replace the log when the caller already holds this log's file lock."""
        rows = list(events)
        for event in rows:
            warning = self._validate_event(event, 0)
            if warning:
                raise ValueError(warning)
        data = "".join(json_line(event) for event in rows).encode("utf-8")
        if self.max_bytes is not None and len(data) > self.max_bytes:
            raise ValueError(f"{self.source_name} is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def prune_tail(self, max_rows: int) -> None:
        count = max(0, int(max_rows))
        with with_file_lock(self.path):
            try:
                if not self.path.is_file() or count_jsonl_rows(self.path) <= count:
                    return
            except OSError:
                return
            read = self.read_locked()
            if read.blocked or len(read.rows) <= count:
                return
            self.write_atomic_locked(read.rows[-count:])

    def delete(self) -> None:
        with with_file_lock(self.path):
            delete_file(self.path)

    def _decode_line(
        self,
        line: str,
        index: int,
    ) -> tuple[dict[str, object] | None, str]:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None, f"{self.source_name}:{index}:bad_json"
        warning = self._validate_event(payload, index)
        if warning:
            return None, warning
        return payload, ""

    def _validate_event(self, payload: object, index: int) -> str:
        row = f"{self.source_name}:{index}" if index else self.source_name
        if not isinstance(payload, dict):
            return f"{row}:not_object"
        if payload.get("schema_version") != self.schema_version:
            return f"{row}:unsupported_schema"
        if self.allowed_event_kinds:
            event_kind = str(payload.get("type") or payload.get("kind") or "").strip()
            if event_kind not in self.allowed_event_kinds:
                return f"{row}:unsupported_event"
        if self.event_validator is not None and not self.event_validator(payload):
            return f"{row}:invalid_event"
        return ""

    def _quarantine_tail(
        self,
        lines: list[str],
        rows: list[dict[str, object]],
        warnings: list[str],
        first_bad_index: int,
    ) -> GhostEventRead:
        tail = lines[first_bad_index - 1 :]
        for offset, line in enumerate(tail[1:], start=first_bad_index + 1):
            if not line.strip():
                continue
            _payload, warning = self._decode_line(line, offset)
            if not warning:
                warnings.append(f"{self.source_name}:{first_bad_index}:mid_file_corruption")
                return GhostEventRead((), tuple(warnings[: self.max_warnings]), True)
        quarantine = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.corrupt")
        try:
            quarantine.write_text("\n".join(tail) + "\n", encoding="utf-8")
            self.write_atomic(
                row
                for row in rows
                if isinstance(row, dict)
            )
            warnings.append(f"{self.source_name}:{first_bad_index}:tail_quarantined")
            return GhostEventRead(tuple(rows), tuple(warnings[: self.max_warnings]))
        except (OSError, TypeError, ValueError):
            warnings.append(f"{self.source_name}:{first_bad_index}:quarantine_failed")
            return GhostEventRead((), tuple(warnings[: self.max_warnings]), True)


def json_line(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ) + "\n"


def count_jsonl_rows(path: str | Path) -> int:
    count = 0
    last_byte = b""
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            count += chunk.count(b"\n")
            last_byte = chunk[-1:]
    if last_byte and last_byte != b"\n":
        count += 1
    return count


def _tail_lines(path: Path, max_rows: int, size: int) -> list[str]:
    chunks: list[bytes] = []
    remaining = max(0, int(size))
    newlines = 0
    with path.open("rb") as handle:
        while remaining > 0 and newlines <= max_rows:
            step = min(8192, remaining)
            remaining -= step
            handle.seek(remaining)
            chunk = handle.read(step)
            chunks.append(chunk)
            newlines += chunk.count(b"\n")
    data = b"".join(reversed(chunks))
    return [
        line.decode("utf-8")
        for line in data.splitlines()[-max_rows:]
    ]
