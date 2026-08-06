"""Append-only Ghost signal candidate event log.

This store records extractor outputs for audit and later inbox processing.  It
does not mark any signal as accepted long-term memory.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from codey.ghost.schema import (
    SCHEMA_VERSION,
    GhostSignalParseResult,
    clip_signal_text,
)
from codey.local_store import DEFAULT_STATE_HOME, delete_file


MAX_GHOST_EVENTS = 5_000
MAX_STORED_SIGNALS = 5
MAX_STORED_DIAGNOSTICS = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GhostSignalStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.path = self.directory / "signals.jsonl"

    def append_extraction(
        self,
        result: GhostSignalParseResult,
        *,
        session_id: str = "",
        run_id: str = "",
        project: str = "",
    ) -> bool:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "ts": _now(),
            "type": "ghost_signal_extraction",
            "session_id": clip_signal_text(session_id, 120),
            "run_id": clip_signal_text(run_id, 120),
            "project": clip_signal_text(project, 240),
            "ok": bool(result.ok),
            "provider_id": clip_signal_text(result.provider_id, 80),
            "raw_text_chars": max(0, int(result.raw_text_chars or 0)),
            "signals": [
                signal.to_payload()
                for signal in result.signals[:MAX_STORED_SIGNALS]
            ],
            "diagnostics": [
                clip_signal_text(item, 240)
                for item in result.diagnostics[:MAX_STORED_DIAGNOSTICS]
            ],
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ) + "\n")
            self._prune()
        except (OSError, TypeError, ValueError):
            return False
        return True

    def read_recent(self, limit: int = 20) -> tuple[dict[str, object], ...]:
        rows = self._read_rows()
        count = max(0, int(limit or 0))
        if count == 0:
            return ()
        return tuple(rows[-count:])

    def delete_all(self) -> None:
        delete_file(self.path)

    def _read_rows(self) -> list[dict[str, object]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        rows: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION:
                rows.append(value)
        return rows

    def _prune(self) -> None:
        rows = self._read_rows()
        if len(rows) <= MAX_GHOST_EVENTS:
            return
        try:
            with self.path.open("w", encoding="utf-8", newline="\n") as handle:
                for row in rows[-MAX_GHOST_EVENTS:]:
                    handle.write(json.dumps(
                        row,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ) + "\n")
        except OSError:
            pass
