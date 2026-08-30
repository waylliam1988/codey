"""Shared JSONL event log primitives for Ghost stores."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid
from typing import Iterable

from codey.storage.local_store import delete_file


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
    ) -> None:
        self.path = Path(path)
        self.schema_version = int(schema_version)
        self.max_bytes = max_bytes
        self.max_warnings = max(0, int(max_warnings))
        self.source_name = source_name or self.path.name

    def read(self) -> GhostEventRead:
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
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"{self.source_name}:{index}:bad_json")
                continue
            if not isinstance(payload, dict):
                warnings.append(f"{self.source_name}:{index}:not_object")
                continue
            if payload.get("schema_version") != self.schema_version:
                warnings.append(f"{self.source_name}:{index}:unsupported_schema")
                continue
            rows.append(payload)
        return GhostEventRead(tuple(rows), tuple(warnings[: self.max_warnings]))

    def append(self, events: Iterable[dict[str, object]]) -> bool:
        rows = [event for event in events if isinstance(event, dict)]
        if not rows:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                for event in rows:
                    handle.write(json_line(event))
            return True
        except (OSError, TypeError, ValueError):
            return False

    def write_atomic(self, events: Iterable[dict[str, object]]) -> None:
        rows = [event for event in events if isinstance(event, dict)]
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
        read = self.read()
        if read.blocked or len(read.rows) <= count:
            return
        self.write_atomic(read.rows[-count:])

    def delete(self) -> None:
        delete_file(self.path)


def json_line(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"

