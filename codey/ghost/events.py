"""Pure Ghost event envelopes and bounded compaction summaries."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

from codey.utils.refs import coerce_int


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, coerce_int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def compact_result_payload(
    ok: bool,
    compacted: bool,
    before: Mapping[str, object],
    after: Mapping[str, object],
    warnings: Iterable[object],
    *,
    warning_cleaner: Callable[[Iterable[object]], Iterable[object]] | None = None,
) -> dict[str, object]:
    clean_warnings = warning_cleaner(warnings) if warning_cleaner is not None else warnings
    before_map = before if isinstance(before, dict) else {}
    after_map = after if isinstance(after, dict) else {}
    return {
        "ok": ok,
        "compacted": compacted,
        "events_before": _safe_count(before_map.get("events")),
        "events_after": _safe_count(after_map.get("events")),
        "bytes_before": _safe_count(before_map.get("bytes")),
        "bytes_after": _safe_count(after_map.get("bytes")),
        "warnings": list(clean_warnings),
    }


def control_event(
    *,
    schema_version: int,
    event_name: str,
    payload: Mapping[str, object],
    now: str,
    event_field: str = "type",
    timestamp_field: str = "ts",
    event_id_prefix: str = "",
    payload_cleaner: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    if type(schema_version) is not int:
        raise ValueError(f"schema_version must be int, got {schema_version!r}")
    event: dict[str, object] = {
        "schema_version": schema_version,
        event_field: event_name,
        timestamp_field: now,
    }
    if event_id_prefix:
        event["event_id"] = f"{event_id_prefix}{uuid.uuid4().hex[:24]}"
    event["payload"] = dict(payload_cleaner(payload) if payload_cleaner else payload)
    return event


def over_compact_budget(
    stats: Mapping[str, object],
    *,
    max_events: int,
    max_bytes: int,
) -> bool:
    """True when an ``event_file_stats`` reading exceeds either budget."""
    try:
        events = int(cast(Any, stats.get("events") or 0))
        size = int(cast(Any, stats.get("bytes") or 0))
    except (TypeError, ValueError):
        return True
    return events > max(0, int(cast(Any, max_events or 0))) or size > max(0, int(cast(Any, max_bytes or 0)))
