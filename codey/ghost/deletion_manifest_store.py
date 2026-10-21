"""Durable owner for Ghost control-plane deletion manifests.

The manifest is deliberately separate from every Ghost projection store. It
records the operation and each store transition so a process restart can
resume pending stores without pretending that the stores share one SQLite
transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from codey.ghost.deletion_manifest import (
    DeletionManifest,
    DeletionManifestError,
    replay,
    started_event,
)
from codey.ghost.sqlite import GhostSQLite


def _encode(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DeletionManifestStore:
    """Persist one canonical event stream per deletion operation."""

    def __init__(self, database: GhostSQLite) -> None:
        self.database = database
        self._connection: ContextVar[sqlite3.Connection | None] = ContextVar(
            "ghost_deletion_manifest_transaction", default=None,
        )

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        if self._connection.get() is not None:
            yield
            return
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._connection.set(connection)
            try:
                yield
            finally:
                self._connection.reset(token)

    def _db(self) -> sqlite3.Connection:
        connection = self._connection.get()
        if connection is None:
            raise RuntimeError("deletion manifest access requires its transaction")
        return connection

    def start(self, manifest: DeletionManifest) -> DeletionManifest:
        """Create or idempotently reopen a manifest with the same operation id."""
        with self.transaction():
            database = self._db()
            existing = database.execute(
                "SELECT manifest FROM ghost_deletion_manifests WHERE operation_id=?",
                (manifest.operation_id,),
            ).fetchone()
            if existing is not None:
                current = DeletionManifest.from_payload(json.loads(existing[0]))
                if not _same_identity(current, manifest):
                    raise DeletionManifestError("operation id already names a different manifest")
                return self._replay_unlocked(manifest.operation_id)
            event = started_event(manifest)
            database.execute(
                "INSERT INTO ghost_deletion_manifests(operation_id,manifest,status,updated_at) VALUES(?,?,?,?)",
                (manifest.operation_id, _encode(manifest.to_payload()), manifest.status, manifest.requested_at),
            )
            self._append_event_unlocked(manifest.operation_id, event)
            return manifest

    def append(self, event: Mapping[str, object]) -> DeletionManifest:
        """Append one transition, accepting an identical retry exactly once."""
        operation_id = event.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise DeletionManifestError("manifest event operation_id is required")
        with self.transaction():
            manifest = self._replay_unlocked(operation_id)
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                raise DeletionManifestError("manifest event event_id is required")
            existing = self._db().execute(
                "SELECT payload FROM ghost_deletion_manifest_events WHERE operation_id=? AND event_id=?",
                (operation_id, event_id),
            ).fetchone()
            encoded = _encode(dict(event))
            if existing is not None:
                if existing[0] != encoded:
                    raise DeletionManifestError("manifest event identity conflict")
                return manifest
            updated = replay(self._events_unlocked(operation_id) + [dict(event)])
            self._append_event_unlocked(operation_id, event)
            self._db().execute(
                "UPDATE ghost_deletion_manifests SET manifest=?, status=?, updated_at=? WHERE operation_id=?",
                (_encode(updated.to_payload()), updated.status, updated.requested_at, operation_id),
            )
            return updated

    def get(self, operation_id: str) -> DeletionManifest:
        with self.transaction():
            return self._replay_unlocked(operation_id)

    def events(self, operation_id: str) -> tuple[dict[str, object], ...]:
        with self.transaction():
            return tuple(self._events_unlocked(operation_id))

    def _replay_unlocked(self, operation_id: str) -> DeletionManifest:
        row = self._db().execute(
            "SELECT manifest FROM ghost_deletion_manifests WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DeletionManifestError("deletion manifest not found")
        manifest = replay(self._events_unlocked(operation_id))
        stored = DeletionManifest.from_payload(json.loads(row[0]))
        if stored.to_payload() != manifest.to_payload():
            raise DeletionManifestError("stored manifest disagrees with its event stream")
        return manifest

    def _events_unlocked(self, operation_id: str) -> list[dict[str, object]]:
        rows = self._db().execute(
            "SELECT payload FROM ghost_deletion_manifest_events WHERE operation_id=? ORDER BY sequence",
            (operation_id,),
        ).fetchall()
        if not rows:
            raise DeletionManifestError("deletion manifest event log is empty")
        return [json.loads(row[0]) for row in rows]

    def _append_event_unlocked(self, operation_id: str, event: Mapping[str, object]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise DeletionManifestError("manifest event event_id is required")
        self._db().execute(
            "INSERT INTO ghost_deletion_manifest_events(operation_id,event_id,payload) VALUES(?,?,?)",
            (operation_id, event_id, _encode(dict(event))),
        )


def _same_identity(first: DeletionManifest, second: DeletionManifest) -> bool:
    return (
        first.operation_id == second.operation_id
        and first.action == second.action
        and first.scope == second.scope
        and first.project == second.project
        and first.session_id == second.session_id
        and first.requested_at == second.requested_at
        and first.retention_class == second.retention_class
        and first.tombstone_digest == second.tombstone_digest
        and tuple(item.store for item in first.stores) == tuple(item.store for item in second.stores)
    )


__all__ = ["DeletionManifestStore"]
