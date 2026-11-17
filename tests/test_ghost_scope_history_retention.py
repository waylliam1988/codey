"""Ordinary tombstone deletion preserves the domain's original audit events."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

from codey.ghost.continuity import GhostContinuityItem, _item_event
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_sources import new_item
from codey.storage.local_store import session_key
from tests.manual.ghost_memory_routing.deletion_runtime_probe import _seed

STORES = ("inbox", "hebbian", "continuity", "work_queue", "affinity", "sleep", "observations")
NOW = "2026-11-17T00:00:00Z"


def _seed_history(home: Path, scope: str) -> GhostControlSurface:
    surface = GhostControlSurface.from_state_home(home)
    project = str(home / "project")
    _seed(surface, project, scope=scope)
    assert surface.continuity is not None and surface.work_queue is not None
    assert surface.affinity is not None and surface.sleep is not None
    scope_ref = {"user": "", "project": project, "session": "session"}[scope]
    context = GhostContinuityItem(
        id="history-context", kind="open_question", scope=scope, scope_ref=scope_ref,
        text="Which recovery step needs review?", source="research_note", source_ref="history-note",
        weight=0.8, confidence=0.9, created_at=NOW, updated_at=NOW,
    )
    with surface.continuity.records.transaction():
        surface.continuity.records.append((_item_event(context, action="upsert"),))
        surface.continuity.records.replace_rows((context.to_payload(),))
    work = new_item(
        kind="open_question", status="queued", scope=scope,
        scope_ref=session_key("session") if scope == "session" else scope_ref,
        title="Review the saved recovery question", why_now="User requested a bounded follow-up.",
        priority=0.8, confidence=0.9, source="user", source_ref="history-user-followup",
        evidence_refs=(), run_refs=(), now=NOW,
    )
    with surface.work_queue.records.transaction():
        surface.work_queue.records.append((_observed_event(work, ts=NOW),))
        surface.work_queue.records.replace_rows((work.to_payload(),))
    assert surface.affinity.sync_from_sources(
        hebbian_store=surface.hebbian, work_queue_store=surface.work_queue,
        session_id="session", project=project,
    ).ok
    surface.sleep.run_once(run_id="history-run", session_id="session", project=project)
    return surface


def _history(surface: GhostControlSurface, store: str) -> tuple[tuple[object, ...], ...]:
    assert surface.observations is not None
    with surface.observations.database.connection() as connection:
        if store in {"hebbian", "affinity"}:
            cursor = connection.execute(
                "SELECT sequence,event_id,payload FROM graph_events WHERE graph=? ORDER BY sequence", (store,),
            )
        elif store == "observations":
            cursor = connection.execute("SELECT * FROM observation_events ORDER BY run_id")
        else:
            assert store in {"inbox", "continuity", "work_queue", "sleep"}
            cursor = connection.execute(
                "SELECT sequence,event_id,payload FROM memory_events WHERE collection=? ORDER BY sequence", (store,),
            )
        rows: Iterable[sqlite3.Row] = cursor
        result: list[tuple[object, ...]] = []
        for row in rows:
            fields: list[object] = []
            for index in range(len(row)):
                field: object = row[index]
                fields.append(field)
            result.append(tuple(fields))
        return tuple(result)


def _visible(surface: GhostControlSurface, store: str) -> bool:
    assert surface.inbox is not None and surface.hebbian is not None and surface.continuity is not None
    assert surface.work_queue is not None and surface.affinity is not None
    assert surface.sleep is not None and surface.observations is not None
    project = str(surface.observations.database.path.parent / "project")
    if store == "inbox":
        return bool(surface.inbox.list_candidates())
    if store == "hebbian":
        return bool(surface.hebbian.list_nodes())
    if store == "continuity":
        return bool(surface.continuity.list_items(session_id="session", project=project))
    if store == "work_queue":
        return bool(surface.work_queue.list_items())
    if store == "affinity":
        return bool(surface.affinity.list_nodes(session_id="session", project=project))
    if store == "sleep":
        return bool(surface.sleep.export_state()["sleep"])
    assert store == "observations"
    return bool(surface.observations.read_committed(scope="user"))


def _rebuild(surface: GhostControlSurface) -> None:
    assert surface.inbox is not None and surface.hebbian is not None and surface.continuity is not None
    assert surface.work_queue is not None and surface.affinity is not None
    with surface.inbox.records.transaction():
        candidates = surface.inbox._rebuild_candidates_from_events_unlocked()
        assert candidates is not None
        surface.inbox._write_state(candidates)
    assert surface.hebbian.rebuild_from_events()
    assert surface.continuity.rebuild_from_events()
    assert surface.work_queue.rebuild_from_events()
    assert surface.affinity.rebuild_from_events()


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("scope", ["user", "project", "session"])
def test_reading_keeps_seeded_domain_history(tmp_path: Path, store: str, scope: str) -> None:
    surface = _seed_history(tmp_path, scope)
    before = _history(surface, store)
    assert before and _visible(surface, store), "invalid history fixture"
    assert _history(surface, store) == before


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("rebuild", [False, True])
def test_ordinary_scope_delete_preserves_original_domain_history(
    tmp_path: Path, store: str, scope: str, rebuild: bool,
) -> None:
    surface = _seed_history(tmp_path, scope)
    before = _history(surface, store)
    assert before and _visible(surface, store), "invalid history fixture"
    status, payload = surface.dispatch_action({
        "action": "delete_scope", "scope": scope, "project": str(tmp_path / "project"),
        "session_id": "session", "confirm": True, "delete_operation_id": "history-delete",
    })
    assert status == 200, payload
    manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
    assert manifest.status == "completed" and manifest.retention_class == "tombstone"
    assert not _visible(surface, store), "ordinary deletion did not hide the seeded source"
    reopened = GhostControlSurface.from_state_home(tmp_path)
    if rebuild:
        _rebuild(reopened)
    assert not _visible(reopened, store), "deleted source returned after reopen/rebuild"
    after = _history(reopened, store)
    assert after[:len(before)] == before, "ordinary tombstone delete rewrote original domain history"
