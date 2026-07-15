"""Bounded JSONL audit trail for local self-repair attempts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codey.local_store import DEFAULT_STATE_HOME


MAX_JOURNAL_BYTES = 512 * 1024
MAX_RECORD_CHARS = 8_000


class RepairJournal:
    """Append small, scrubbed repair records without storing user content."""

    def __init__(self, state_home: str | Path | None = DEFAULT_STATE_HOME) -> None:
        self.path = Path(state_home) / "self-repair" / "journal.jsonl" if state_home else None

    def append(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        record = {
            "event": _safe_text(event, 80),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        for key, value in fields.items():
            if key in {"prompt", "reply", "url", "cookie", "source", "content"}:
                continue
            record[_safe_text(key, 80)] = _scrub(value)
        data = json.dumps(record, ensure_ascii=False, sort_keys=True)
        if len(data) > MAX_RECORD_CHARS:
            data = json.dumps(
                {
                    "event": record["event"],
                    "time": record["time"],
                    "truncated": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_JOURNAL_BYTES:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(data + "\n")
        except OSError:
            pass


def _safe_text(value: object, limit: int) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()[:limit]


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_text(value, 500)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value[:20]]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            text_key = _safe_text(key, 80)
            if text_key.lower() in {"prompt", "reply", "url", "cookie", "source", "content"}:
                continue
            result[text_key] = _scrub(item)
        return result
    return _safe_text(value, 500)
