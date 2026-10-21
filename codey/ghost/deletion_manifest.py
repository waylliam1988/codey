"""Deterministic deletion manifest candidate for Ghost control operations.

This module is deliberately pure.  It models the control-plane record needed
to make a multi-store delete or reset resumable before wiring it into the
existing stores.  It never performs I/O and never treats a projection as the
authoritative deletion record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace

MANIFEST_SCHEMA_VERSION = 1
STORE_PENDING = "pending"
STORE_COMPLETED = "completed"
STORE_BLOCKED = "blocked"
MANIFEST_PENDING = "pending"
MANIFEST_COMPLETED = "completed"
MANIFEST_BLOCKED = "blocked"
_STORE_STATES = frozenset({STORE_PENDING, STORE_COMPLETED, STORE_BLOCKED})
_ACTIONS = frozenset({"delete_scope", "reset_all"})
_SCOPES = frozenset({"user", "project", "session", "all"})
_MAX_TEXT = 240


class DeletionManifestError(ValueError):
    """Raised when a manifest or its event stream cannot be trusted."""


@dataclass(frozen=True)
class StoreDeletionStatus:
    store: str
    status: str = STORE_PENDING
    detail: str = ""

    def __post_init__(self) -> None:
        store = _text(self.store, "store")
        status = _text(self.status, "store status")
        if status not in _STORE_STATES:
            raise DeletionManifestError(f"unsupported store status: {status}")
        object.__setattr__(self, "store", store)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "detail", _bounded(self.detail))

    def to_payload(self) -> dict[str, str]:
        payload = {"store": self.store, "status": self.status}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class DeletionManifest:
    operation_id: str
    action: str
    scope: str
    requested_at: str
    retention_class: str
    tombstone_digest: str
    stores: tuple[StoreDeletionStatus, ...]
    project: str = ""
    session_id: str = ""
    status: str = MANIFEST_PENDING

    def __post_init__(self) -> None:
        operation_id = _text(self.operation_id, "operation_id")
        action = _text(self.action, "action")
        scope = _text(self.scope, "scope")
        requested_at = _text(self.requested_at, "requested_at")
        retention_class = _text(self.retention_class, "retention_class")
        digest = _text(self.tombstone_digest, "tombstone_digest")
        status = _text(self.status, "manifest status")
        if action not in _ACTIONS:
            raise DeletionManifestError(f"unsupported action: {action}")
        if scope not in _SCOPES or (action == "delete_scope" and scope == "all"):
            raise DeletionManifestError(f"unsupported scope for {action}: {scope}")
        if action == "reset_all" and scope != "all":
            raise DeletionManifestError("reset_all requires all scope")
        if status not in {MANIFEST_PENDING, MANIFEST_COMPLETED, MANIFEST_BLOCKED}:
            raise DeletionManifestError(f"unsupported manifest status: {status}")
        stores = _unique_stores(self.stores)
        if not stores:
            raise DeletionManifestError("manifest requires at least one store")
        if status == MANIFEST_COMPLETED and any(item.status != STORE_COMPLETED for item in stores):
            raise DeletionManifestError("completed manifest has unfinished stores")
        if status == MANIFEST_BLOCKED and not any(item.status == STORE_BLOCKED for item in stores):
            raise DeletionManifestError("blocked manifest has no blocked store")
        object.__setattr__(self, "operation_id", operation_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "retention_class", retention_class)
        object.__setattr__(self, "tombstone_digest", digest)
        object.__setattr__(self, "stores", stores)
        object.__setattr__(self, "project", _bounded(self.project))
        object.__setattr__(self, "session_id", _bounded(self.session_id))
        object.__setattr__(self, "status", status)

    @property
    def pending_stores(self) -> tuple[str, ...]:
        return tuple(item.store for item in self.stores if item.status == STORE_PENDING)

    def store(self, name: str) -> StoreDeletionStatus:
        for item in self.stores:
            if item.store == name:
                return item
        raise DeletionManifestError(f"unknown store: {name}")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "operation_id": self.operation_id,
            "action": self.action,
            "scope": self.scope,
            "project": self.project,
            "session_id": self.session_id,
            "requested_at": self.requested_at,
            "retention_class": self.retention_class,
            "tombstone_digest": self.tombstone_digest,
            "status": self.status,
            "stores": [item.to_payload() for item in self.stores],
        }

    @classmethod
    def from_payload(cls, payload: object) -> DeletionManifest:
        if not isinstance(payload, Mapping):
            raise DeletionManifestError("manifest payload must be an object")
        if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise DeletionManifestError("unsupported manifest schema")
        raw_stores = payload.get("stores")
        if not isinstance(raw_stores, list):
            raise DeletionManifestError("manifest stores must be a list")
        stores = tuple(_status_from_payload(item) for item in raw_stores)
        return cls(
            operation_id=_required(payload, "operation_id"),
            action=_required(payload, "action"),
            scope=_required(payload, "scope"),
            project=_optional(payload, "project"),
            session_id=_optional(payload, "session_id"),
            requested_at=_required(payload, "requested_at"),
            retention_class=_required(payload, "retention_class"),
            tombstone_digest=_required(payload, "tombstone_digest"),
            status=_required(payload, "status"),
            stores=stores,
        )


def new_manifest(
    *,
    operation_id: str,
    action: str,
    scope: str,
    requested_at: str,
    retention_class: str,
    stores: tuple[str, ...],
    project: str = "",
    session_id: str = "",
) -> DeletionManifest:
    """Create the single canonical manifest for one control operation."""
    canonical = {
        "action": action,
        "scope": scope,
        "project": _bounded(project),
        "session_id": _bounded(session_id),
        "retention_class": retention_class,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    return DeletionManifest(
        operation_id=operation_id,
        action=action,
        scope=scope,
        project=project,
        session_id=session_id,
        requested_at=requested_at,
        retention_class=retention_class,
        tombstone_digest=digest,
        stores=tuple(StoreDeletionStatus(store=name) for name in stores),
    )


def store_event(manifest: DeletionManifest, store: str, status: str, *, detail: str = "") -> dict[str, object]:
    """Return a deterministic event for one store transition."""
    current = manifest.store(store)
    if current.status != STORE_PENDING and (current.status != status or _bounded(current.detail) != _bounded(detail)):
        raise DeletionManifestError(f"conflicting transition for completed store: {store}")
    if status not in _STORE_STATES or status == STORE_PENDING:
        raise DeletionManifestError("store event must complete or block a pending store")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "event_id": f"{manifest.operation_id}:store:{store}:{status}",
        "kind": "store_transition",
        "operation_id": manifest.operation_id,
        "store": store,
        "status": status,
        "detail": _bounded(detail),
    }


def started_event(manifest: DeletionManifest) -> dict[str, object]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "event_id": f"{manifest.operation_id}:started",
        "kind": "started",
        "operation_id": manifest.operation_id,
        "manifest": manifest.to_payload(),
    }


def apply_store_event(manifest: DeletionManifest, event: Mapping[str, object]) -> DeletionManifest:
    """Apply one validated store event; duplicate transitions are idempotent."""
    if event.get("schema_version") != MANIFEST_SCHEMA_VERSION or event.get("kind") != "store_transition":
        raise DeletionManifestError("invalid store transition event")
    if event.get("operation_id") != manifest.operation_id:
        raise DeletionManifestError("event operation does not match manifest")
    store = _required(event, "store")
    status = _required(event, "status")
    detail = _optional(event, "detail")
    current = manifest.store(store)
    if current.status != STORE_PENDING:
        if current.status == status and current.detail == _bounded(detail):
            return manifest
        raise DeletionManifestError(f"conflicting transition for store: {store}")
    if status not in {STORE_COMPLETED, STORE_BLOCKED}:
        raise DeletionManifestError("store transition must complete or block")
    updated = tuple(
        replace(item, status=status, detail=detail) if item.store == store else item for item in manifest.stores
    )
    overall = MANIFEST_BLOCKED if status == STORE_BLOCKED else MANIFEST_PENDING
    if all(item.status == STORE_COMPLETED for item in updated):
        overall = MANIFEST_COMPLETED
    return replace(manifest, stores=updated, status=overall)


def replay(events: list[Mapping[str, object]]) -> DeletionManifest:
    """Rebuild a manifest from its event log and fail closed on corruption."""
    if not events:
        raise DeletionManifestError("deletion manifest event log is empty")
    first = events[0]
    if first.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise DeletionManifestError("unsupported manifest schema")
    if first.get("kind") != "started":
        raise DeletionManifestError("deletion manifest must start with a started event")
    payload = first.get("manifest")
    manifest = DeletionManifest.from_payload(payload)
    first_event_id = first.get("event_id")
    if not isinstance(first_event_id, str) or not first_event_id:
        raise DeletionManifestError("started event has no event id")
    if first.get("operation_id") != manifest.operation_id:
        raise DeletionManifestError("started event operation does not match manifest")
    seen: dict[str, Mapping[str, object]] = {first_event_id: first}
    for event in events[1:]:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise DeletionManifestError("duplicate or missing manifest event id")
        prior = seen.get(event_id)
        if prior is not None:
            if dict(event) == dict(prior):
                continue
            raise DeletionManifestError("duplicate or missing manifest event id")
        seen[event_id] = event
        manifest = apply_store_event(manifest, event)
    return manifest


def _status_from_payload(payload: object) -> StoreDeletionStatus:
    if not isinstance(payload, Mapping):
        raise DeletionManifestError("store status must be an object")
    return StoreDeletionStatus(
        store=_required(payload, "store"),
        status=_required(payload, "status"),
        detail=_optional(payload, "detail"),
    )


def _unique_stores(stores: tuple[StoreDeletionStatus, ...]) -> tuple[StoreDeletionStatus, ...]:
    seen: set[str] = set()
    result: list[StoreDeletionStatus] = []
    for item in stores:
        if item.store in seen:
            raise DeletionManifestError(f"duplicate store: {item.store}")
        seen.add(item.store)
        result.append(item)
    return tuple(result)


def _required(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeletionManifestError(f"{key} must be a non-empty string")
    return value


def _optional(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise DeletionManifestError(f"{key} must be a string")
    return _bounded(value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeletionManifestError(f"{label} must be a non-empty string")
    return value.strip()


def _bounded(value: object) -> str:
    return str(value or "").strip()[:_MAX_TEXT]


__all__ = [
    "DeletionManifest",
    "DeletionManifestError",
    "MANIFEST_BLOCKED",
    "MANIFEST_COMPLETED",
    "MANIFEST_PENDING",
    "MANIFEST_SCHEMA_VERSION",
    "STORE_BLOCKED",
    "STORE_COMPLETED",
    "STORE_PENDING",
    "StoreDeletionStatus",
    "apply_store_event",
    "new_manifest",
    "replay",
    "started_event",
    "store_event",
]
