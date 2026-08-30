"""Append-only Ghost signal candidate event log.

This store records extractor outputs for audit and later inbox processing.  It
does not mark any signal as accepted long-term memory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from codey.ghost.event_log import GhostEventLog
from codey.ghost.schema import (
    SCHEMA_VERSION,
    GhostSignalParseResult,
    clip_signal_text,
)
from codey.storage.local_store import DEFAULT_STATE_HOME


MAX_GHOST_EVENTS = 5_000
MAX_STORED_SIGNALS = 5
MAX_STORED_DIAGNOSTICS = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class GhostSignalStore:
    def __init__(self, state_home: str | Path = DEFAULT_STATE_HOME) -> None:
        self.directory = Path(state_home) / "ghost"
        self.path = self.directory / "signals.jsonl"
        self.log = GhostEventLog(
            self.path,
            schema_version=SCHEMA_VERSION,
            source_name="signals.jsonl",
            allowed_event_kinds=("ghost_signal_extraction",),
            bad_row_policy="warn",
        )
        self.last_warnings: tuple[str, ...] = ()

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
        if not self.log.append((payload,)):
            return False
        self._prune()
        return True

    def read_recent(self, limit: int = 20) -> tuple[dict[str, object], ...]:
        rows = self._read_rows()
        count = max(0, int(limit or 0))
        if count == 0:
            return ()
        return tuple(rows[-count:])

    def read_all(self) -> tuple[dict[str, object], ...]:
        return tuple(self._read_rows())

    def delete_all(self) -> None:
        self.log.delete()

    def delete_scope(
        self,
        scope: str,
        *,
        project: str = "",
        session_id: str = "",
    ) -> int:
        normalized_scope = str(scope or "").strip().lower()
        if normalized_scope not in {"user", "project", "session"}:
            raise ValueError("scope must be user, project, or session")
        normalized_project = _normalize_project(project)
        normalized_session = clip_signal_text(session_id, 120)
        if normalized_scope == "project" and not normalized_project:
            raise ValueError("project is required for project scope deletion")
        if normalized_scope == "session" and not normalized_session:
            raise ValueError("session_id is required for session scope deletion")
        rows = self._read_rows()
        removed = 0
        rewritten: list[dict[str, object]] = []
        for row in rows:
            signals = row.get("signals")
            if not isinstance(signals, list):
                rewritten.append(row)
                continue
            kept_signals = []
            for signal in signals:
                if _signal_scope_match(
                    signal,
                    normalized_scope,
                    row_project=_normalize_project(row.get("project")),
                    row_session=clip_signal_text(row.get("session_id"), 120),
                    project=normalized_project,
                    session_id=normalized_session,
                ):
                    removed += 1
                else:
                    kept_signals.append(signal)
            clean_row = dict(row)
            clean_row["signals"] = kept_signals
            if kept_signals:
                rewritten.append(clean_row)
        if removed:
            self.log.write_atomic(rewritten)
        return removed

    def _read_rows(self) -> list[dict[str, object]]:
        read = self.log.read()
        self.last_warnings = read.warnings
        return list(read.rows)

    def _prune(self) -> None:
        try:
            self.log.prune_tail(MAX_GHOST_EVENTS)
        except OSError:
            pass


def _signal_scope_match(
    signal: object,
    scope: str,
    *,
    row_project: str,
    row_session: str,
    project: str,
    session_id: str,
) -> bool:
    if not isinstance(signal, dict):
        return False
    if str(signal.get("scope") or "").strip().lower() != scope:
        return False
    if scope == "project":
        return bool(project) and row_project == project
    if scope == "session":
        return bool(session_id) and row_session == session_id
    return True


def _normalize_project(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return clip_signal_text(Path(text).expanduser().resolve(), 240)
    except (OSError, RuntimeError, ValueError):
        return clip_signal_text(text, 240)
