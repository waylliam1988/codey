"""Frozen real-store deletion/writer interleavings; no model or user state."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Protocol

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import new_manifest
from codey.ghost.deletion_manifest_coordinator import (
    GHOST_STORE_NAMES,
    DeletionManifestCoordinator,
    DeletionTarget,
)
from codey.ghost.deletion_manifest_store import DeletionManifestStore
from codey.ghost.inbox import GhostMemoryCandidate
from codey.ghost.schema import GhostSignal


class ScopeStore(Protocol):
    def delete_scope(self, scope: str, *, project: str = "", session_id: str = "") -> object: ...
    def reset_all(self) -> object: ...


@dataclass(frozen=True)
class Cell:
    action: str
    writer: str
    order: str
    repetition: int
    manifest_status: str
    writer_applied: bool
    resurrected: bool
    rebuild_ok: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "action": self.action, "writer": self.writer, "order": self.order,
            "repetition": self.repetition, "manifest_status": self.manifest_status,
            "writer_applied": self.writer_applied, "resurrected": self.resurrected,
            "rebuild_ok": self.rebuild_ok,
        }


def _targets(surface: GhostControlSurface) -> dict[str, DeletionTarget]:
    assert surface.inbox is not None
    stores: dict[str, ScopeStore | None] = {
        "hebbian": surface.hebbian,
        "continuity": surface.continuity,
        "sleep": surface.sleep,
        "work_queue": surface.work_queue,
        "affinity": surface.affinity,
        "observations": surface.observations,
    }
    targets = {"inbox": DeletionTarget(surface.inbox.delete_scope, surface.inbox.reset_all)}
    for name, store in stores.items():
        assert store is not None
        targets[name] = DeletionTarget(store.delete_scope, store.reset_all)
    return targets


def _seed(
    surface: GhostControlSurface, project: str, *, run_id: str = "old-run", scope: str = "project",
) -> GhostMemoryCandidate:
    assert surface.inbox is not None
    assert surface.hebbian is not None
    assert surface.observations is not None
    created = surface.inbox.ingest_signals(
        (GhostSignal(
            kind="style_preference", scope=scope, summary="Concise answers.",
            evidence_quote="Use concise answers.", confidence=1.0, source="manual",
            metadata={"conflict_key": "reply_length", "value_key": "concise"},
        ),),
        session_id="session", run_id=run_id, project=project, user_text="Use concise answers.",
    )
    assert len(created) == 1
    candidate = surface.inbox.review_candidate(created[0].id, "accept", reviewed_by="probe")
    assert candidate is not None
    assert surface.hebbian.reinforce_candidate(candidate).applied
    assert surface.observations.append_completed(
        run_id=run_id, session_id="session", project=project, mode="chat",
        user_text="Use concise answers.", assistant_text="Done.", stop_reason="done",
    )
    return candidate


def _cell(home: Path, action: str, writer: str, order: str, repetition: int) -> Cell:
    surface = GhostControlSurface.from_state_home(home)
    project = str(home / "project")
    candidate = _seed(surface, project)
    assert surface.observations is not None
    coordinator = DeletionManifestCoordinator(
        DeletionManifestStore(surface.observations.database), _targets(surface),
    )
    manifest = new_manifest(
        operation_id="deletion-probe", action=action,
        scope="all" if action == "reset_all" else "project", project=project,
        session_id="session", requested_at="2026-11-17T00:00:00Z",
        retention_class="tombstone", stores=GHOST_STORE_NAMES,
    )
    ready, release = Event(), Event()

    def delayed_writer() -> bool:
        # Open an independent store owner and retain the pre-delete input.
        owner = GhostControlSurface.from_state_home(home)
        ready.set()
        if not release.wait(15):
            raise TimeoutError("delete did not release writer")
        if writer == "hebbian":
            assert owner.hebbian is not None
            return owner.hebbian.reinforce_candidate(candidate).applied
        assert owner.observations is not None
        return owner.observations.append_completed(
            run_id="old-run", session_id="session", project=project, mode="chat",
            user_text="Use concise answers.", assistant_text="Done.", stop_reason="done",
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(delayed_writer)
        try:
            if not ready.wait(15):
                raise TimeoutError("writer did not acquire input")
            if order == "writer_first":
                release.set()
                applied = pending.result(timeout=15)
                completed = coordinator.run(manifest)
            else:
                completed = coordinator.run(manifest)
                release.set()
                applied = pending.result(timeout=15)
        finally:
            release.set()
    restarted = GhostControlSurface.from_state_home(home)
    assert restarted.hebbian is not None
    assert restarted.observations is not None
    rebuilt = restarted.hebbian.rebuild_from_events()
    resurrected = (
        bool(restarted.hebbian.list_nodes()) if writer == "hebbian"
        else bool(restarted.observations.read_committed(project=project))
    )
    return Cell(action, writer, order, repetition, completed.status, applied, resurrected, rebuilt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output: object = args.output
    if not isinstance(output, Path):
        raise TypeError("output must be a path")
    cells: list[Cell] = []
    with TemporaryDirectory(prefix="codey-deletion-runtime-") as temporary:
        root = Path(temporary)
        for action in ("delete_scope", "reset_all"):
            for writer in ("hebbian", "observations"):
                for order in ("writer_first", "delete_first"):
                    for repetition in range(2):
                        cells.append(_cell(root / str(len(cells)), action, writer, order, repetition))
    payload = {
        "schema_version": 1, "model": None, "cells": [cell.to_payload() for cell in cells],
        "cell_count": len(cells), "resurrections": sum(cell.resurrected for cell in cells),
        "all_manifests_completed": all(cell.manifest_status == "completed" for cell in cells),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "cells"}))


if __name__ == "__main__":
    main()
