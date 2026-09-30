"""Ghost work-queue events: construction, validation, and pure replay.

Owns event shapes and the deterministic ``items <- events`` projection.
Pure over already-loaded payloads: no file locks, no atomic writes, no
model calls. The work-queue store owns persistence and transactions
and delegates to this module.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import replace

from codey.ghost import _common
from codey.ghost._warnings import bounded_warnings, event_read_warnings
from codey.ghost.schema import clip_signal_text, contains_sensitive_signal_text
from codey.ghost.work_queue_model import (
    KIND_PRIORITY,
    MAX_WORK_ITEMS,
    MAX_WORK_WARNINGS,
    STATUS_PRIORITY,
    TERMINAL_STATUSES,
    WORK_QUEUE_SCHEMA_VERSION,
    GhostWorkItem,
    _bounded_refs,
    _clean_metadata,
    _clean_scope,
    _clean_status,
    _int,
    _list,
    _research_proof_ref,
    _valid_work_item_payload,
)

_PROJECTION_KIND = "ghost_work_items_projection"

_WORK_EVENT_TYPES = frozenset(
    {
        "ghost_work_item_observed",
        "ghost_work_item_transitioned",
        "ghost_work_items_deleted",
        "ghost_work_snapshot",
    }
)

WORK_ITEM_TRANSITION_MATRIX = {
    "claim": {"queued": frozenset({"running"})},
    "complete": {"running": frozenset({"done"})},
    "block": {
        "candidate": frozenset({"blocked"}),
        "queued": frozenset({"blocked"}),
        "running": frozenset({"blocked"}),
    },
    "release": {"running": frozenset({"queued", "blocked"})},
    "release_stale": {"running": frozenset({"queued", "blocked"})},
    "reject": {
        "candidate": frozenset({"rejected"}),
        "queued": frozenset({"rejected"}),
        "blocked": frozenset({"rejected"}),
    },
    "queue": {
        "candidate": frozenset({"queued"}),
        "blocked": frozenset({"queued"}),
        "rejected": frozenset({"queued"}),
    },
}
WORK_ITEM_TRANSITION_ACTIONS = frozenset(WORK_ITEM_TRANSITION_MATRIX)
_WORK_PRECONDITION_KEYS = frozenset({"expected_status", "expected_started_run_id", "expected_retry_count"})
_WORK_DELETE_PAYLOAD_KEYS = frozenset({"reason", "item_ids", "expected_items", "scope", "project_ref", "session_ref"})
_WORK_EVENT_KEYS = {
    "ghost_work_item_observed": frozenset({"schema_version", "type", "event_id", "ts", "item"}),
    "ghost_work_item_transitioned": frozenset(
        {"schema_version", "type", "event_id", "ts", "action", "item_id", "precondition", "patch"}
    ),
    "ghost_work_items_deleted": frozenset({"schema_version", "type", "event_id", "ts", "payload"}),
    "ghost_work_snapshot": frozenset({"schema_version", "type", "event_id", "ts", "reason", "items"}),
}
_WORK_TRANSITION_PATCH_KEYS = {
    "claim": frozenset({"status", "started_run_id", "retry_count", "lease_expires_at", "blocked_reason", "updated_at"}),
    "complete": frozenset(
        {"status", "completed_run_id", "proof_refs", "lease_expires_at", "blocked_reason", "updated_at"}
    ),
    "release": frozenset({"status", "started_run_id", "lease_expires_at", "blocked_reason", "updated_at"}),
    "release_stale": frozenset({"status", "started_run_id", "lease_expires_at", "blocked_reason", "updated_at"}),
    "block": frozenset({"status", "blocked_reason", "lease_expires_at", "updated_at"}),
    "reject": frozenset(
        {
            "status",
            "started_run_id",
            "completed_run_id",
            "proof_refs",
            "lease_expires_at",
            "blocked_reason",
            "updated_at",
        }
    ),
    "queue": frozenset(
        {
            "status",
            "started_run_id",
            "completed_run_id",
            "proof_refs",
            "retry_count",
            "lease_expires_at",
            "blocked_reason",
            "updated_at",
        }
    ),
}


def items_from_events(events: Iterable[dict[str, object]]) -> list[GhostWorkItem]:
    """Replay one event sequence into work items (deterministic, pure)."""
    by_id: dict[str, GhostWorkItem] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        now = _common.event_ts(event)
        if event_type == "ghost_work_snapshot":
            by_id = {item.id: item for item in _snapshot_items(event)}
        elif event_type == "ghost_work_item_observed":
            item = GhostWorkItem.from_payload(event.get("item"))
            if item is None:
                continue
            item = replace(item, created_at=item.created_at or now, updated_at=item.updated_at or now)
            merged, _changed = _merge_items(by_id.values(), (item,), now=now)
            by_id = {row.id: row for row in _bounded_items(merged)}
        elif event_type == "ghost_work_item_transitioned":
            _apply_transition_event(by_id, event, now=now)
        elif event_type == "ghost_work_items_deleted":
            _apply_items_deleted_event(by_id, event)
    return list(by_id.values())


def _snapshot_items(event: Mapping[str, object]) -> list[GhostWorkItem]:
    return _bounded_items(
        item for item in (GhostWorkItem.from_payload(row) for row in _list(event.get("items"))) if item is not None
    )


def _projection_payload(
    items: Iterable[GhostWorkItem],
    *,
    generated_at: str,
    warnings: Iterable[str],
) -> dict[str, object]:
    rows = _bounded_items(items)
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "kind": _PROJECTION_KIND,
        "generated_at": generated_at,
        "items": [item.to_payload() for item in rows],
        "warnings": list(_bounded_warnings(warnings)),
    }


def _observed_event(item: GhostWorkItem, *, ts: str) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_item_observed",
        "event_id": "gwe_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "item": item.to_payload(),
    }


def _transition_event(
    current: GhostWorkItem,
    *,
    action: str,
    patch: Mapping[str, object],
    ts: str,
) -> dict[str, object]:
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_item_transitioned",
        "event_id": "gwe_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "action": clip_signal_text(action, 40),
        "item_id": clip_signal_text(current.id, 120),
        "precondition": {
            "expected_status": current.status,
            "expected_started_run_id": current.started_run_id,
            "expected_retry_count": current.retry_count,
        },
        "patch": _transition_patch_payload(patch),
    }


def _items_deleted_event(
    *,
    reason: str,
    ts: str,
    item_ids: Iterable[object] = (),
    expected_items: Iterable[GhostWorkItem] = (),
    scope: str = "",
    project_ref: str = "",
    session_ref: str = "",
) -> dict[str, object]:
    expected = [
        {
            "id": item.id,
            "expected_status": item.status,
            "expected_started_run_id": item.started_run_id,
            "expected_retry_count": item.retry_count,
        }
        for item in expected_items
        if isinstance(item, GhostWorkItem)
    ]
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_items_deleted",
        "event_id": "gwd_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "payload": {
            "reason": clip_signal_text(reason, 80),
            "item_ids": [item_id for item_id in (clip_signal_text(value, 120) for value in item_ids) if item_id],
            "expected_items": expected,
            "scope": _clean_scope(scope),
            "project_ref": clip_signal_text(project_ref, 120),
            "session_ref": clip_signal_text(session_ref, 120),
        },
    }


def _snapshot_event(
    items: Iterable[GhostWorkItem],
    *,
    ts: str,
    reason: str,
) -> dict[str, object]:
    rows = _bounded_items(items)
    return {
        "schema_version": WORK_QUEUE_SCHEMA_VERSION,
        "type": "ghost_work_snapshot",
        "event_id": "gws_" + uuid.uuid4().hex[:24],
        "ts": clip_signal_text(ts, 80),
        "reason": clip_signal_text(reason, 80),
        "items": [item.to_payload() for item in rows],
    }


def _merge_items(
    existing: Iterable[GhostWorkItem],
    incoming: Iterable[GhostWorkItem],
    *,
    now: str,
) -> tuple[list[GhostWorkItem], list[GhostWorkItem]]:
    by_id = {item.id: item for item in existing}
    changed: list[GhostWorkItem] = []
    for item in incoming:
        if not item.title:
            continue
        current = by_id.get(item.id)
        if current is None:
            by_id[item.id] = item
            changed.append(item)
            continue
        if current.status in TERMINAL_STATUSES:
            continue
        status = current.status
        if current.status == "candidate" and item.status == "queued":
            status = "queued"
        merged = replace(
            current,
            status=status,
            title=item.title,
            why_now=item.why_now or current.why_now,
            priority=max(current.priority, item.priority),
            confidence=max(current.confidence, item.confidence),
            evidence_refs=_bounded_refs((*current.evidence_refs, *item.evidence_refs)),
            run_refs=_bounded_refs((*current.run_refs, *item.run_refs)),
            updated_at=now,
            metadata={**dict(current.metadata), **dict(item.metadata)},
        )
        if _meaningful_item_payload(merged) == _meaningful_item_payload(current):
            continue
        by_id[item.id] = merged
        changed.append(merged)
    return list(by_id.values()), changed


def _bounded_items(items: Iterable[GhostWorkItem]) -> list[GhostWorkItem]:
    rows = [item for item in items if isinstance(item, GhostWorkItem)]
    return sorted(rows, key=_item_sort_key)[:MAX_WORK_ITEMS]


def _item_sort_key(item: GhostWorkItem) -> tuple[int, int, float, tuple[int, ...], str]:
    return (
        STATUS_PRIORITY.get(item.status, 99),
        KIND_PRIORITY.get(item.kind, 99),
        -item.priority,
        _common.reverse_text_sort_key(item.updated_at),
        item.id,
    )


def _meaningful_item_payload(item: GhostWorkItem) -> tuple[object, ...]:
    return (
        item.kind,
        item.status,
        item.scope,
        item.scope_ref,
        item.title,
        item.why_now,
        round(item.priority, 6),
        round(item.confidence, 6),
        item.source,
        item.source_ref,
        item.evidence_refs,
        item.run_refs,
        item.started_run_id,
        item.completed_run_id,
        item.proof_refs,
        item.blocked_reason,
        item.retry_count,
        item.lease_expires_at,
        tuple(sorted(_clean_metadata(item.metadata).items())),
    )


def _item_payloads(items: Iterable[GhostWorkItem]) -> tuple[tuple[object, ...], ...]:
    return tuple(_meaningful_item_payload(item) for item in _bounded_items(items))


def _valid_work_event(event: Mapping[str, object]) -> bool:
    if not clip_signal_text(event.get("event_id"), 120):
        return False
    if not clip_signal_text(event.get("ts"), 80):
        return False
    event_type = str(event.get("type") or "")
    if not _common.mapping_keys_within(event, _WORK_EVENT_KEYS.get(event_type, ())):
        return False
    if event_type == "ghost_work_snapshot":
        return _valid_work_snapshot(event)
    if event_type == "ghost_work_item_observed":
        return _valid_work_item_payload(event.get("item"))
    if event_type == "ghost_work_item_transitioned":
        return _valid_work_transition(event)
    if event_type == "ghost_work_items_deleted":
        payload = event.get("payload")
        if not _valid_work_delete_payload(payload):
            return False
        reason = str(payload.get("reason") or "") if isinstance(payload, Mapping) else ""
        item_ids = payload.get("item_ids") if isinstance(payload, Mapping) else []
        expected_items = payload.get("expected_items") if isinstance(payload, Mapping) else []
        scope = str(payload.get("scope") or "") if isinstance(payload, Mapping) else ""
        project_ref = str(payload.get("project_ref") or "") if isinstance(payload, Mapping) else ""
        session_ref = str(payload.get("session_ref") or "") if isinstance(payload, Mapping) else ""
        if reason == "expired":
            if item_ids or scope or project_ref or session_ref:
                return False
            return bool(expected_items) and all(
                _valid_work_precondition(row, require_id=True) for row in expected_items
            )
        if reason == "scope_deleted":
            if item_ids or expected_items:
                return False
            if scope == "user":
                return not project_ref and not session_ref
            if scope == "project":
                return bool(project_ref and not session_ref)
            if scope == "session":
                return bool(session_ref and not project_ref)
            return False
        return False
    return False


def _valid_claim_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
    expected_started_run_id: str,
    expected_retry_count: object,
) -> bool:
    if target_status != "running" or expected_status != "queued":
        return False
    if expected_started_run_id:
        return False
    started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
    lease_expires_at = clip_signal_text(patch.get("lease_expires_at"), 80)
    if not started_run_id or not lease_expires_at:
        return False
    if "retry_count" not in patch:
        return False
    retry_count = patch["retry_count"]
    if not _common.valid_nonnegative_int_payload(retry_count):
        return False
    if retry_count < 1 or retry_count != expected_retry_count + 1:
        return False
    if clip_signal_text(patch.get("blocked_reason"), 120):
        return False
    if clip_signal_text(patch.get("completed_run_id"), 120):
        return False
    return not _bounded_refs(patch.get("proof_refs"))


def _valid_complete_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
    expected_started_run_id: str,
) -> bool:
    if target_status != "done" or expected_status != "running":
        return False
    completed_run_id = clip_signal_text(patch.get("completed_run_id"), 120)
    if not expected_started_run_id or not completed_run_id or completed_run_id != expected_started_run_id:
        return False
    proof_refs = _bounded_refs(patch.get("proof_refs"))
    if not proof_refs:
        return False
    if clip_signal_text(patch.get("lease_expires_at"), 80):
        return False
    return not clip_signal_text(patch.get("blocked_reason"), 120)


def _valid_release_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
    expected_started_run_id: str,
) -> bool:
    if target_status not in {"queued", "blocked"} or expected_status != "running":
        return False
    if not expected_started_run_id:
        return False
    if clip_signal_text(patch.get("lease_expires_at"), 80):
        return False
    if clip_signal_text(patch.get("completed_run_id"), 120):
        return False
    if _bounded_refs(patch.get("proof_refs")):
        return False
    if target_status == "queued":
        if clip_signal_text(patch.get("started_run_id"), 120):
            return False
        if clip_signal_text(patch.get("blocked_reason"), 120):
            return False
    elif target_status == "blocked":
        if not clip_signal_text(patch.get("blocked_reason"), 120):
            return False
        started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
        if started_run_id and started_run_id != expected_started_run_id:
            return False
    return True


def _valid_block_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
) -> bool:
    if target_status != "blocked" or expected_status not in {"running", "queued", "candidate"}:
        return False
    if not clip_signal_text(patch.get("blocked_reason"), 120):
        return False
    if clip_signal_text(patch.get("lease_expires_at"), 80):
        return False
    if clip_signal_text(patch.get("completed_run_id"), 120):
        return False
    return not _bounded_refs(patch.get("proof_refs"))


def _valid_reject_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
) -> bool:
    if target_status != "rejected" or expected_status not in {"candidate", "queued", "blocked"}:
        return False
    if clip_signal_text(patch.get("lease_expires_at"), 80):
        return False
    if clip_signal_text(patch.get("blocked_reason"), 120):
        return False
    if clip_signal_text(patch.get("started_run_id"), 120):
        return False
    if clip_signal_text(patch.get("completed_run_id"), 120):
        return False
    return not _bounded_refs(patch.get("proof_refs"))


def _valid_queue_transition(
    patch: Mapping[str, object],
    target_status: str,
    expected_status: str,
) -> bool:
    if target_status != "queued" or expected_status not in {"candidate", "blocked", "rejected"}:
        return False
    if "retry_count" not in patch:
        return False
    retry_count = patch["retry_count"]
    if not _common.valid_nonnegative_int_payload(retry_count) or retry_count != 0:
        return False
    if clip_signal_text(patch.get("started_run_id"), 120):
        return False
    if clip_signal_text(patch.get("completed_run_id"), 120):
        return False
    if _bounded_refs(patch.get("proof_refs")):
        return False
    if clip_signal_text(patch.get("blocked_reason"), 120):
        return False
    return not clip_signal_text(patch.get("lease_expires_at"), 80)


def _valid_work_transition(event: Mapping[str, object]) -> bool:
    item_id = clip_signal_text(event.get("item_id"), 120)
    action = clip_signal_text(event.get("action"), 40)
    precondition = event.get("precondition")
    patch = event.get("patch")
    if not item_id or action not in WORK_ITEM_TRANSITION_ACTIONS:
        return False
    if not _valid_work_precondition(precondition) or not isinstance(patch, Mapping):
        return False
    if not _common.mapping_keys_within(patch, _WORK_TRANSITION_PATCH_KEYS.get(action, ())):
        return False
    target_status = _clean_status(patch.get("status"))
    if not target_status:
        return False
    updated_at = clip_signal_text(patch.get("updated_at"), 80)
    if not updated_at:
        return False
    expected_status = _clean_status(precondition.get("expected_status"))
    expected_started_run_id = clip_signal_text(precondition.get("expected_started_run_id"), 120)
    expected_retry_count = precondition.get("expected_retry_count")
    if not _common.valid_nonnegative_int_payload(expected_retry_count):
        return False
    if action == "claim":
        return _valid_claim_transition(
            patch, target_status, expected_status, expected_started_run_id, expected_retry_count
        )
    if action == "complete":
        return _valid_complete_transition(patch, target_status, expected_status, expected_started_run_id)
    if action in {"release", "release_stale"}:
        return _valid_release_transition(patch, target_status, expected_status, expected_started_run_id)
    if action == "block":
        return _valid_block_transition(patch, target_status, expected_status)
    if action == "reject":
        return _valid_reject_transition(patch, target_status, expected_status)
    if action == "queue":
        return _valid_queue_transition(patch, target_status, expected_status)
    return False


def _valid_work_snapshot(event: Mapping[str, object]) -> bool:
    raw_items = event.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_WORK_ITEMS:
        return False
    item_ids: set[str] = set()
    for row in raw_items:
        item = GhostWorkItem.from_payload(row)
        if item is None or item.id in item_ids:
            return False
        if not _valid_work_item_payload(row):
            return False
        item_ids.add(item.id)
    return True


def _valid_work_precondition(value: object, *, require_id: bool = False) -> bool:
    if not isinstance(value, Mapping):
        return False
    allowed = _WORK_PRECONDITION_KEYS | (frozenset({"id"}) if require_id else frozenset())
    if set(value.keys()) != allowed:
        return False
    if require_id and not _valid_canonical_text(value.get("id"), 120, required=True):
        return False
    expected_status = value.get("expected_status")
    if not isinstance(expected_status, str) or _clean_status(expected_status) != expected_status:
        return False
    if not _valid_canonical_text(value.get("expected_started_run_id"), 120):
        return False
    return _common.valid_nonnegative_int_payload(value.get("expected_retry_count"))


def _valid_work_delete_payload(payload: object) -> bool:
    if not isinstance(payload, Mapping) or set(payload.keys()) != _WORK_DELETE_PAYLOAD_KEYS:
        return False
    reason = payload.get("reason")
    if not isinstance(reason, str) or reason not in {"expired", "scope_deleted"}:
        return False
    item_ids = payload.get("item_ids")
    expected_items = payload.get("expected_items")
    if not _valid_ref_list_payload(item_ids) or not isinstance(expected_items, list):
        return False
    if not all(_valid_work_precondition(row, require_id=True) for row in expected_items):
        return False
    scope = payload.get("scope")
    project_ref = payload.get("project_ref")
    session_ref = payload.get("session_ref")
    if not _valid_scope_payload(scope):
        return False
    if not _valid_canonical_text(project_ref, 120) or not _valid_canonical_text(session_ref, 120):
        return False
    if reason == "expired":
        return item_ids == [] and bool(expected_items) and scope == "" and project_ref == "" and session_ref == ""
    if item_ids or expected_items:
        return False
    if scope == "user":
        return project_ref == "" and session_ref == ""
    if scope == "project":
        return bool(project_ref) and session_ref == ""
    if scope == "session":
        return bool(session_ref) and project_ref == ""
    return False


def _valid_ref_list_payload(value: object) -> bool:
    return isinstance(value, list) and list(_bounded_refs(value)) == value


def _valid_scope_payload(value: object) -> bool:
    from codey.ghost.work_queue_model import _clean_scope as _clean_scope_value

    return isinstance(value, str) and (value == "" or _clean_scope_value(value) == value)


def _valid_canonical_text(value: object, limit: int, *, required: bool = False) -> bool:
    if not isinstance(value, str):
        return False
    if required and not value:
        return False
    return clip_signal_text(value, limit) == value and not contains_sensitive_signal_text(value)


def _transition_patch_payload(patch: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    if "status" in patch:
        status = _clean_status(patch.get("status"))
        if status:
            out["status"] = status
    if "started_run_id" in patch:
        out["started_run_id"] = clip_signal_text(patch.get("started_run_id"), 120)
    if "completed_run_id" in patch:
        out["completed_run_id"] = clip_signal_text(patch.get("completed_run_id"), 120)
    if "proof_refs" in patch:
        out["proof_refs"] = list(_bounded_refs(patch.get("proof_refs")))
    if "blocked_reason" in patch:
        out["blocked_reason"] = clip_signal_text(patch.get("blocked_reason"), 120)
    if "retry_count" in patch:
        out["retry_count"] = max(0, _int(patch.get("retry_count")))
    if "lease_expires_at" in patch:
        out["lease_expires_at"] = clip_signal_text(patch.get("lease_expires_at"), 80)
    if "updated_at" in patch:
        out["updated_at"] = clip_signal_text(patch.get("updated_at"), 80)
    return out


def _apply_claim_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    *,
    now: str,
) -> GhostWorkItem | None:
    started_run_id = clip_signal_text(patch.get("started_run_id"), 120)
    lease_expires_at = clip_signal_text(patch.get("lease_expires_at"), 80)
    if not started_run_id or not lease_expires_at or "retry_count" not in patch:
        return None
    retry_count = patch["retry_count"]
    if not _common.valid_nonnegative_int_payload(retry_count):
        return None
    if retry_count < 1 or retry_count != current.retry_count + 1:
        return None
    return replace(
        current,
        status="running",
        started_run_id=started_run_id,
        retry_count=retry_count,
        lease_expires_at=lease_expires_at,
        completed_run_id="",
        proof_refs=(),
        blocked_reason="",
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_complete_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    *,
    now: str,
) -> GhostWorkItem | None:
    completed_run_id = clip_signal_text(patch.get("completed_run_id"), 120)
    if not completed_run_id or not current.started_run_id or completed_run_id != current.started_run_id:
        return None
    proof_refs = _bounded_refs(patch.get("proof_refs"))
    if not proof_refs or not _primary_proof_matches_item_kind(current, proof_refs):
        return None
    return replace(
        current,
        status="done",
        completed_run_id=completed_run_id,
        proof_refs=proof_refs,
        blocked_reason="",
        lease_expires_at="",
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_release_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    target_status: str,
    *,
    now: str,
) -> GhostWorkItem | None:
    blocked_reason = clip_signal_text(patch.get("blocked_reason"), 120) if target_status == "blocked" else ""
    if target_status == "blocked" and not blocked_reason:
        return None
    return replace(
        current,
        status=target_status,
        started_run_id="" if target_status == "queued" else current.started_run_id,
        completed_run_id="",
        proof_refs=(),
        lease_expires_at="",
        blocked_reason=blocked_reason,
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_block_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    *,
    now: str,
) -> GhostWorkItem | None:
    blocked_reason = clip_signal_text(patch.get("blocked_reason"), 120)
    if not blocked_reason:
        return None
    return replace(
        current,
        status="blocked",
        blocked_reason=blocked_reason,
        completed_run_id="",
        proof_refs=(),
        lease_expires_at="",
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_reject_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    *,
    now: str,
) -> GhostWorkItem | None:
    return replace(
        current,
        status="rejected",
        started_run_id="",
        completed_run_id="",
        proof_refs=(),
        lease_expires_at="",
        blocked_reason="",
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_queue_transition(
    current: GhostWorkItem,
    patch: Mapping[str, object],
    *,
    now: str,
) -> GhostWorkItem | None:
    if "retry_count" not in patch:
        return None
    try:
        if int(patch["retry_count"]) != 0:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return replace(
        current,
        status="queued",
        retry_count=0,
        started_run_id="",
        completed_run_id="",
        proof_refs=(),
        blocked_reason="",
        lease_expires_at="",
        updated_at=clip_signal_text(patch.get("updated_at"), 80) or now,
    )


def _apply_transition_event(
    by_id: dict[str, GhostWorkItem],
    event: Mapping[str, object],
    *,
    now: str,
) -> str:
    item_id = clip_signal_text(event.get("item_id"), 120)
    current = by_id.get(item_id)
    if current is None:
        return "stale"
    precondition = event.get("precondition")
    patch = event.get("patch")
    if not isinstance(precondition, Mapping) or not isinstance(patch, Mapping):
        return "invalid"
    if not _precondition_matches(current, precondition):
        return "stale"
    target_status = _clean_status(patch.get("status"))
    action = clip_signal_text(event.get("action"), 40)
    if not target_status or not _transition_allowed(current, target_status, action=action):
        return "invalid"
    if action == "claim":
        updated = _apply_claim_transition(current, patch, now=now)
    elif action == "complete":
        updated = _apply_complete_transition(current, patch, now=now)
    elif action in {"release", "release_stale"}:
        updated = _apply_release_transition(current, patch, target_status, now=now)
    elif action == "block":
        updated = _apply_block_transition(current, patch, now=now)
    elif action == "reject":
        updated = _apply_reject_transition(current, patch, now=now)
    elif action == "queue":
        updated = _apply_queue_transition(current, patch, now=now)
    else:
        return "invalid"
    if updated is None:
        return "invalid"
    if GhostWorkItem.from_payload(updated.to_payload()) is None:
        return "invalid"
    by_id[updated.id] = updated
    return "applied"


def _apply_items_deleted_event(by_id: dict[str, GhostWorkItem], event: Mapping[str, object]) -> None:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return
    item_ids = {
        item_id for item_id in (clip_signal_text(value, 120) for value in _list(payload.get("item_ids"))) if item_id
    }
    expected_items = [row for row in _list(payload.get("expected_items")) if isinstance(row, Mapping)]
    if expected_items:
        for row in expected_items:
            item_id = clip_signal_text(row.get("id"), 120)
            current = by_id.get(item_id)
            if current is not None and _precondition_matches(current, row):
                by_id.pop(item_id, None)
        return
    if item_ids:
        for item_id in item_ids:
            by_id.pop(item_id, None)
        return
    scope = _clean_scope(payload.get("scope"))
    project_ref = clip_signal_text(payload.get("project_ref"), 120)
    session_ref = clip_signal_text(payload.get("session_ref"), 120)
    if not scope:
        return
    for item_id, item in list(by_id.items()):
        if _scope_filter_matches(item, scope=scope, project_ref=project_ref, session_ref=session_ref):
            by_id.pop(item_id, None)


def _precondition_matches(item: GhostWorkItem, precondition: Mapping[str, object]) -> bool:
    expected_status = _clean_status(precondition.get("expected_status"))
    if expected_status and item.status != expected_status:
        return False
    if "expected_started_run_id" in precondition:
        expected_run_id = clip_signal_text(precondition.get("expected_started_run_id"), 120)
        if item.started_run_id != expected_run_id:
            return False
    return not (
        "expected_retry_count" in precondition
        and item.retry_count != max(0, _int(precondition.get("expected_retry_count")))
    )


def _transition_allowed(item: GhostWorkItem, target_status: str, *, action: str) -> bool:
    return target_status in WORK_ITEM_TRANSITION_MATRIX.get(action, {}).get(item.status, frozenset())


def _scope_matches(item: GhostWorkItem, *, project_ref: str, session_ref: str) -> bool:
    from codey.ghost.work_queue_model import _project_ref as _model_project_ref

    if item.scope == "session":
        return bool(session_ref and item.scope_ref == session_ref)
    if item.scope == "project":
        return bool(project_ref and _model_project_ref(item.scope_ref) == project_ref)
    return item.scope == "user"


def _scope_filter_matches(
    item: GhostWorkItem,
    *,
    scope: str,
    project_ref: str,
    session_ref: str,
) -> bool:
    from codey.ghost.work_queue_model import _project_ref as _model_project_ref

    normalized_scope = str(scope or "").strip().lower()
    if not normalized_scope:
        return (
            _scope_matches(item, project_ref=project_ref, session_ref=session_ref)
            if (project_ref or session_ref)
            else True
        )
    if item.scope != normalized_scope:
        return False
    if normalized_scope == "project":
        return bool(project_ref and _model_project_ref(item.scope_ref) == project_ref)
    if normalized_scope == "session":
        return bool(session_ref and item.scope_ref == session_ref)
    return True


def _receipt_proves_project_work(receipt: Mapping[str, object]) -> bool:
    work = receipt.get("work")
    return isinstance(work, Mapping) and _int(work.get("changed_count")) > 0


def _primary_proof_matches_item_kind(item: GhostWorkItem, refs: Iterable[str]) -> bool:
    prefixes = {str(ref).split(":", 1)[0] for ref in refs}
    if item.kind in {"research", "open_question"}:
        return any(_research_proof_ref(ref) for ref in refs)
    if item.kind == "review":
        return "review" in prefixes
    if item.kind in {"coding", "project_followup"}:
        return bool(prefixes.intersection({"diff", "receipt"}))
    return False


def _bounded_warnings(warnings: Iterable[object]) -> tuple[str, ...]:
    return bounded_warnings(warnings, limit=MAX_WORK_WARNINGS, redact_sensitive=True)


def _event_read_warnings(warnings: Iterable[str]) -> tuple[str, ...]:
    return event_read_warnings(
        warnings, stream="work_events", limit=MAX_WORK_WARNINGS, redact_sensitive=True
    )


_items_from_events = items_from_events


__all__ = [
    "WORK_ITEM_TRANSITION_ACTIONS",
    "WORK_ITEM_TRANSITION_MATRIX",
    "items_from_events",
]
