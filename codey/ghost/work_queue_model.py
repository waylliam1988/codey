"""Ghost work-queue model: items, constants, and field rules (leaf).

Owns work-item shape, deterministic identity, and payload validation. No
store, file locks, or event-log dependencies: persistence and transactions
stay in ``codey.ghost.work_queue``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from codey.ghost import _common
from codey.ghost.graph_primitives import parse_ts
from codey.ghost.numbers import clamp_unit_float
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.policies.prompt_safety import is_prompt_visible_text_safe
from codey.storage.local_store import project_key, session_key
from codey.utils.refs import coerce_int, generated_ref

WORK_QUEUE_SCHEMA_VERSION = 1


MAX_WORK_ITEMS = 200


MAX_WORK_EVENTS = 5_000


MAX_WORK_STATE_BYTES = 1024 * 1024


MAX_WORK_EVENTS_BYTES = 1024 * 1024


MAX_WORK_TITLE_CHARS = 140


MAX_WORK_WHY_CHARS = 240


MAX_WORK_REF_CHARS = 160


MAX_WORK_REFS = 10


MAX_WORK_WARNINGS = 20


MAX_WORK_RETRIES = 3


DEFAULT_WORK_CLAIM_LEASE_SECONDS = 24 * 60 * 60


WORK_ITEM_KINDS = frozenset(
    {
        "research",
        "coding",
        "review",
        "memory_sleep",
        "open_question",
        "project_followup",
    }
)


WORK_ITEM_STATUSES = frozenset(
    {
        "candidate",
        "queued",
        "running",
        "blocked",
        "done",
        "rejected",
        "expired",
    }
)


WORK_ITEM_SOURCES = frozenset(
    {
        "continuity",
        "concept_open_question",
        "research_note",
        "research_interest",
        "run_ledger",
        "review",
        "work_checkpoint",
        "sleep",
        "user",
    }
)


ACTIVE_STATUSES = frozenset({"candidate", "queued", "running", "blocked"})


CLAIMABLE_STATUSES = frozenset({"queued"})


TERMINAL_STATUSES = frozenset({"done", "rejected", "expired"})


TASKRUNNER_KINDS = frozenset({"research", "open_question", "coding", "project_followup", "review"})


KIND_PRIORITY = {
    "project_followup": 0,
    "coding": 1,
    "review": 2,
    "research": 3,
    "open_question": 4,
    "memory_sleep": 5,
}


STATUS_PRIORITY = {
    "running": 0,
    "queued": 1,
    "blocked": 2,
    "candidate": 3,
    "done": 4,
    "rejected": 5,
    "expired": 6,
}


SCOPE_PRIORITY = {
    "session": 0,
    "project": 1,
    "user": 2,
}


@dataclass(frozen=True)
class GhostWorkItem:
    id: str
    kind: str
    status: str
    scope: str
    scope_ref: str
    title: str
    why_now: str
    priority: float
    confidence: float
    source: str
    source_ref: str
    evidence_refs: tuple[str, ...] = ()
    run_refs: tuple[str, ...] = ()
    started_run_id: str = ""
    completed_run_id: str = ""
    proof_refs: tuple[str, ...] = ()
    blocked_reason: str = ""
    retry_count: int = 0
    lease_expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "title": self.title,
            "why_now": self.why_now,
            "priority": self.priority,
            "confidence": self.confidence,
            "source": self.source,
            "source_ref": self.source_ref,
            "evidence_refs": list(self.evidence_refs),
            "run_refs": list(self.run_refs),
            "started_run_id": self.started_run_id,
            "completed_run_id": self.completed_run_id,
            "proof_refs": list(self.proof_refs),
            "blocked_reason": self.blocked_reason,
            "retry_count": self.retry_count,
            "lease_expires_at": self.lease_expires_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": _clean_metadata(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> GhostWorkItem | None:
        if not isinstance(payload, dict):
            return None
        kind = _clean_kind(payload.get("kind"))
        status = _clean_status(payload.get("status"))
        scope = _clean_scope(payload.get("scope"))
        source = _clean_source(payload.get("source"))
        if not kind or not status or not scope or not source:
            return None
        item_id = clip_signal_text(payload.get("id"), 120)
        title = _clean_item_text(payload.get("title"), max_chars=MAX_WORK_TITLE_CHARS)
        why_now = _clean_item_text(payload.get("why_now"), max_chars=MAX_WORK_WHY_CHARS)
        if not item_id or not title:
            return None

        started_run_id = clip_signal_text(payload.get("started_run_id"), 120)
        completed_run_id = clip_signal_text(payload.get("completed_run_id"), 120)
        proof_refs = _bounded_refs(payload.get("proof_refs"))
        blocked_reason = clip_signal_text(payload.get("blocked_reason"), 120)
        lease_expires_at = clip_signal_text(payload.get("lease_expires_at"), 80)

        if status == "done":
            if not completed_run_id or not proof_refs or lease_expires_at or blocked_reason:
                return None
        elif status in {"queued", "candidate", "rejected"}:
            if started_run_id or completed_run_id or proof_refs or lease_expires_at or blocked_reason:
                return None
        elif status == "running":
            if not started_run_id or completed_run_id or proof_refs or blocked_reason:
                return None
        elif status == "blocked":
            if not blocked_reason or lease_expires_at or completed_run_id or proof_refs:
                return None
        else:
            return None

        return cls(
            id=item_id,
            kind=kind,
            status=status,
            scope=scope,
            scope_ref=clip_signal_text(payload.get("scope_ref"), 240),
            title=title,
            why_now=why_now,
            priority=_unit_float(payload.get("priority")),
            confidence=_unit_float(payload.get("confidence")),
            source=source,
            source_ref=clip_signal_text(payload.get("source_ref"), MAX_WORK_REF_CHARS),
            evidence_refs=_bounded_refs(payload.get("evidence_refs")),
            run_refs=_bounded_refs(payload.get("run_refs")),
            started_run_id=started_run_id,
            completed_run_id=completed_run_id,
            proof_refs=proof_refs,
            blocked_reason=blocked_reason,
            retry_count=max(0, _int(payload.get("retry_count"))),
            lease_expires_at=lease_expires_at,
            created_at=clip_signal_text(payload.get("created_at"), 80),
            updated_at=clip_signal_text(payload.get("updated_at"), 80),
            expires_at=clip_signal_text(payload.get("expires_at"), 80),
            metadata=_clean_metadata(payload.get("metadata")),
        )


@dataclass(frozen=True)
class GhostWorkSyncResult:
    ok: bool
    skipped_reason: str = ""
    items_changed: int = 0
    total_items: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GhostWorkClaimResult:
    ok: bool
    item: GhostWorkItem | None = None
    mode: str = ""
    task: str = ""
    skipped_reason: str = ""
    warnings: tuple[str, ...] = ()


def _stable_item_id(
    *,
    kind: str,
    scope: str,
    scope_ref: str,
    source: str,
    source_ref: str,
    title: str,
) -> str:
    key = "|".join(
        (
            kind,
            scope,
            scope_ref,
            source,
            source_ref,
            " ".join(str(title or "").split()).casefold(),
        )
    )
    return "gwi_" + hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]


def _clean_item_text(value: object, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\r\n", "\n").replace("\r", "\n").split())
    text = clip_signal_text(text, max_chars).rstrip(".")
    if not text:
        return ""
    lower = text.casefold()
    if "ghost" in lower or "work queue" in lower or "workitem" in lower:
        return ""
    if contains_sensitive_signal_text(text) or not is_prompt_visible_text_safe(text):
        return ""
    return text


def _clean_kind(value: object) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in WORK_ITEM_KINDS else ""


def _clean_status(value: object) -> str:
    status = str(value or "").strip().lower()
    return status if status in WORK_ITEM_STATUSES else ""


def _clean_scope(value: object) -> str:
    scope = str(value or "").strip().lower()
    return scope if scope in {"user", "project", "session"} else ""


def _clean_source(value: object) -> str:
    source = str(value or "").strip().lower()
    return source if source in WORK_ITEM_SOURCES else ""


def _bounded_refs(values: object) -> tuple[str, ...]:
    out: list[str] = []
    for value in _list(values):
        text = clip_signal_text(value, MAX_WORK_REF_CHARS)
        if not text or contains_sensitive_signal_text(text):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= MAX_WORK_REFS:
            break
    return tuple(out)


def _clean_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, object] = {}
    for key, item in value.items():
        clean_key = clip_signal_text(key, 80)
        if not clean_key or contains_sensitive_signal_text(clean_key):
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            clean_item: object = clip_signal_text(item, 180) if isinstance(item, str) else item
        else:
            clean_item = clip_signal_text(item, 180)
        if isinstance(clean_item, str) and contains_sensitive_signal_text(clean_item):
            continue
        out[clean_key] = clean_item
        if len(out) >= 12:
            break
    return out


def _unit_float(value: object) -> float:
    return clamp_unit_float(value, digits=4)


def _int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, str) and not value.strip().isascii():
        return 0
    try:
        text = value.strip() if isinstance(value, str) else value
        if isinstance(text, str) and not text.isdigit():
            # allow leading +/- and whitespace like int(), but reject
            # fullwidth/unicode digits that int() cannot parse
            stripped = text.strip()
            if stripped[:1] in ("+", "-"):
                stripped = stripped[1:]
            if stripped and not (stripped.isascii() and stripped.isdigit()):
                return 0
        return coerce_int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _list(value: object) -> list[object]:
    if isinstance(value, tuple):
        return list(value)
    return value if isinstance(value, list) else []


def _valid_work_item_payload(payload: object) -> bool:
    item = GhostWorkItem.from_payload(payload)
    return item is not None and _common.strict_payload_equal(payload, item.to_payload())


def _project_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return project_key(text)
    except (OSError, RuntimeError, ValueError):
        return hashlib.sha256(text.casefold().encode("utf-8", errors="replace")).hexdigest()[:24]


def _session_ref(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return session_key(text)


def _field(value: Any, key: str) -> object:
    from codey.ghost._common import field_value

    return field_value(value, key)


def _future_ts(now: str, seconds: int) -> str:
    base = parse_ts(now)
    try:
        delta_seconds = int(seconds)
    except (TypeError, ValueError, OverflowError):
        delta_seconds = DEFAULT_WORK_CLAIM_LEASE_SECONDS
    return (
        datetime.fromtimestamp(
            base.timestamp() + delta_seconds,
            tz=UTC,
        )
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _parse_ts_or_none(value: object) -> datetime | None:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_expired(item: GhostWorkItem, now: str) -> bool:
    if not item.expires_at:
        return False
    return parse_ts(item.expires_at) <= parse_ts(now)


def _is_stale_claim(item: GhostWorkItem, now: str) -> bool:
    if item.status != "running":
        return False
    if not item.lease_expires_at:
        return True
    lease_expires_at = _parse_ts_or_none(item.lease_expires_at)
    if lease_expires_at is None:
        return True
    return lease_expires_at <= parse_ts(now)


def _proof_run_ref(run_id: str) -> str:
    text = clip_signal_text(run_id, 120)
    if not text:
        return ""
    if contains_sensitive_signal_text(text):
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return text


def _research_proof_ref(value: object) -> str:
    return generated_ref(value, "research_proof")


__all__ = [
    "ACTIVE_STATUSES",
    "CLAIMABLE_STATUSES",
    "DEFAULT_WORK_CLAIM_LEASE_SECONDS",
    "KIND_PRIORITY",
    "MAX_WORK_EVENTS",
    "MAX_WORK_EVENTS_BYTES",
    "MAX_WORK_ITEMS",
    "MAX_WORK_REF_CHARS",
    "MAX_WORK_REFS",
    "MAX_WORK_RETRIES",
    "MAX_WORK_STATE_BYTES",
    "MAX_WORK_TITLE_CHARS",
    "MAX_WORK_WARNINGS",
    "MAX_WORK_WHY_CHARS",
    "SCOPE_PRIORITY",
    "STATUS_PRIORITY",
    "TASKRUNNER_KINDS",
    "TERMINAL_STATUSES",
    "WORK_ITEM_KINDS",
    "WORK_ITEM_SOURCES",
    "WORK_ITEM_STATUSES",
    "WORK_QUEUE_SCHEMA_VERSION",
    "GhostWorkClaimResult",
    "GhostWorkItem",
    "GhostWorkSyncResult",
]
