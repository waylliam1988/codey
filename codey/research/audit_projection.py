"""Bounded audit metadata shared by history and Trace; never execution state."""

from __future__ import annotations

from collections.abc import Mapping

from codey.policies.redaction import looks_sensitive_code
from codey.research.audit_result import MAX_AUDIT_DIAGNOSTICS
from codey.runs.text_clip import clip_text


def _items(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=str))
    return ()


def _safe_code(value: object, limit: int) -> str:
    raw = clip_text(value, limit)
    if not raw or looks_sensitive_code(raw):
        return ""
    text = "".join(char if char.isalnum() or char in "._:-" else "_" for char in raw)
    return "" if looks_sensitive_code(text) else text


def project_audit_diagnostics(value: object) -> list[dict[str, object]]:
    """Bound untrusted diagnostics without serializing business data or messages."""
    rows: list[dict[str, object]] = []
    for item in _items(value):
        if not isinstance(item, Mapping):
            continue
        stage = _safe_code(item.get("stage"), 80)
        status = item.get("status")
        phase = item.get("phase")
        reason = _safe_code(item.get("reason_code"), 80)
        if not stage or status not in ("ok", "skipped", "error") or phase not in ("initial", "final"):
            continue
        if status != "ok" and not reason:
            continue
        rows.append({
            "stage": stage, "status": status, "phase": phase, "reason_code": reason,
            "error_type": _safe_code(item.get("error_type"), 80),
        })
        if len(rows) >= MAX_AUDIT_DIAGNOSTICS:
            break
    return rows

