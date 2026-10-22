"""Candidate coordinator for resumable Ghost delete/reset operations.

This module owns only the ordering between the durable manifest and already
existing store mutation methods.  It is not wired into the control surface
until the seven-store replay and failure matrix is complete.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from codey.ghost.deletion_manifest import DeletionManifest, store_event
from codey.ghost.deletion_manifest_store import DeletionManifestStore

GHOST_STORE_NAMES = (
    "inbox",
    "hebbian",
    "continuity",
    "sleep",
    "work_queue",
    "affinity",
    "observations",
)


@dataclass(frozen=True)
class DeletionTarget:
    """One existing store's already-owned mutation entry points."""

    delete_scope: Callable[..., object]
    reset_all: Callable[[], object]


class DeletionManifestCoordinator:
    """Apply pending store transitions in manifest order and stop on failure."""

    def __init__(self, manifests: DeletionManifestStore, targets: Mapping[str, DeletionTarget]) -> None:
        unknown = set(targets) - set(GHOST_STORE_NAMES)
        missing = set(GHOST_STORE_NAMES) - set(targets)
        if unknown or missing:
            raise ValueError(f"deletion targets must be exactly the seven Ghost stores: missing={sorted(missing)}, unknown={sorted(unknown)}")
        self.manifests = manifests
        self.targets = dict(targets)

    def run(
        self,
        manifest: DeletionManifest,
    ) -> DeletionManifest:
        """Apply the durable operation using its manifest scope.

        The manifest is the only source of truth for the operation scope.  A
        caller must not be able to resume the same operation with a different
        project or session and accidentally delete another scope.
        """
        current = self.manifests.start(manifest)
        for name in current.pending_stores:
            target = self.targets[name]
            try:
                if current.action == "reset_all":
                    result = target.reset_all()
                else:
                    result = target.delete_scope(
                        current.scope,
                        project=current.project,
                        session_id=current.session_id,
                    )
                detail = _result_detail(result)
                current = self.manifests.append(store_event(current, name, "completed", detail=detail))
            except Exception as exc:  # noqa: BLE001 - durable blocked transition
                detail = f"{type(exc).__name__}: {exc}"[:240]
                current = self.manifests.append(store_event(current, name, "blocked", detail=detail))
                break
        return current


def _result_detail(value: object) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:240]
    except (TypeError, ValueError):
        return str(value)[:240]


__all__ = ["DeletionManifestCoordinator", "DeletionTarget", "GHOST_STORE_NAMES"]
