"""Ordinary tombstone deletion preserves the domain's original audit events."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from codey.ghost.continuity import GhostContinuityItem, _item_event
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest
from codey.ghost.observations import GhostObservationStore
from codey.ghost.payload_types import mapping_fields
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
            retained: sqlite3.Row | None = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_history'",
            ).fetchone()
            table = "observation_history" if retained is not None else "observation_events"
            cursor = connection.execute(f"SELECT * FROM {table} ORDER BY sequence")
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


@pytest.mark.parametrize("scope", ["user", "project", "session"])
def test_new_operation_counts_only_live_deletions_and_new_sources_remain_usable(tmp_path: Path, scope: str) -> None:
    surface = _seed_history(tmp_path, scope)
    project = str(tmp_path / "project")
    original = {store: _history(surface, store) for store in STORES}
    request: dict[str, object] = {
        "action": "delete_scope", "scope": scope, "project": project, "session_id": "session", "confirm": True,
    }
    assert surface.dispatch_action(request)[0] == 200
    assert surface.observations is not None and surface.sleep is not None
    assert surface.observations.delete_scope(scope, project=project, session_id="session") == 0
    assert surface.sleep.delete_scope(scope, project=project, session_id="session") == {"reports": 0, "state_deleted": 0}
    assert surface.dispatch_action(request)[0] == 200
    candidate = _seed(surface, project, scope=scope, run_id="fresh-run")
    assert surface.inbox is not None and surface.hebbian is not None
    assert surface.inbox.list_candidates()[0].id == candidate.id
    assert [row["run_id"] for row in surface.observations.read_all()] == ["fresh-run"]
    assert not surface.observations.append_completed(
        run_id="old-run", session_id="session", project=project, mode="chat", user_text="old input",
        assistant_text="old output", stop_reason="done",
    )
    assert surface.hebbian.rebuild_from_events()
    assert all(candidate.id in node.candidate_ids for node in surface.hebbian.list_nodes())
    for store in STORES:
        assert _history(surface, store)[:len(original[store])] == original[store]


@pytest.mark.parametrize("scope", ["project", "session"])
def test_sleep_fallback_does_not_restore_a_previously_deleted_report(tmp_path: Path, scope: str) -> None:
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.sleep is not None
    project_a, project_b = str(tmp_path / "a"), str(tmp_path / "b")
    surface.sleep.run_once(run_id="run-a", session_id="a", project=project_a)
    latest = surface.sleep.run_once(run_id="run-b", session_id="b", project=project_b)
    original = _history(surface, "sleep")
    assert surface.sleep.delete_scope(scope, project=project_a, session_id="a")["reports"] == 1
    remaining = surface.sleep.export_state()["sleep"]
    assert mapping_fields(remaining)
    report = remaining["report"]
    assert mapping_fields(report)
    assert report["cycle_id"] == latest.cycle_id
    assert surface.sleep.delete_scope(scope, project=project_b, session_id="b")["reports"] == 1
    assert not surface.sleep.export_state()["sleep"]
    assert _history(surface, "sleep")[:len(original)] == original
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert reopened.sleep is not None and not reopened.sleep.export_state()["sleep"]


@pytest.mark.parametrize("store", ["inbox", "hebbian", "sleep"])
@pytest.mark.parametrize("damage", ["invalid_scope", "missing_payload"])
def test_scope_deletion_replay_refuses_invalid_control_events(tmp_path: Path, store: str, damage: str) -> None:
    surface = _seed_history(tmp_path, "project")
    assert surface.inbox is not None and surface.hebbian is not None and surface.sleep is not None
    event: dict[str, object] = {
        "type": {"inbox": "ghost_memory_scope_deleted", "hebbian": "ghost_hebbian_scope_deleted",
                 "sleep": "ghost_sleep_scope_deleted"}[store],
        "schema_version": 1, "event_id": "invalid-deletion", "ts": NOW,
    }
    if damage == "invalid_scope":
        event["payload"] = {"scope": "unknown"}
    records = {"inbox": surface.inbox.records, "sleep": surface.sleep.records}.get(store)
    if records is not None:
        with records.transaction():
            rows = records.rows()
            records.append((event,))
            records.replace_rows(rows)
    else:
        with surface.hebbian.graph.transaction():
            nodes, edges = surface.hebbian.graph.rows()
            surface.hebbian.graph.append((event,))
            surface.hebbian.graph.replace_rows(nodes, edges)
    before = _history(surface, store)
    if store == "inbox":
        with pytest.raises(ValueError, match="invalid Ghost candidate"), surface.inbox.records.transaction():
            surface.inbox._rebuild_candidates_from_events_unlocked()
    elif store == "hebbian":
        assert not surface.hebbian.rebuild_from_events()
    else:
        with pytest.raises(ValueError, match="invalid Ghost Sleep"):
            surface.sleep.delete_scope("project", project=str(tmp_path / "project"))
    assert _visible(surface, store)
    assert _history(surface, store) == before


class _Embeddings:
    model_id = "retention-test-space"
    dimensions = 2

    def __init__(self, before_encode: Callable[[], None] | None = None) -> None:
        self.before_encode = before_encode

    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if self.before_encode is not None:
            callback = self.before_encode
            self.before_encode = None
            callback()
        return tuple((1.0, 0.0) for _text in texts)


def _embedding_ids(store: GhostObservationStore) -> tuple[str, ...]:
    with store.database.connection() as connection:
        rows: Iterable[sqlite3.Row] = connection.execute("SELECT run_id FROM memory_embeddings ORDER BY run_id")
        result: list[str] = []
        for row in rows:
            identity: object = row[0]
            assert isinstance(identity, str)
            result.append(identity)
        return tuple(result)


def test_retention_preserves_raw_facts_and_removes_only_deleted_scope_vectors(tmp_path: Path) -> None:
    store = GhostObservationStore(tmp_path, embeddings=_Embeddings())
    for identity in ("a", "b"):
        assert store.append_completed(
            run_id=identity, session_id=identity, project=str(tmp_path / identity), mode="chat",
            user_text="Recovery check", assistant_text="Needs review", stop_reason="done",
        )
    assert len(store.retrieve("Recovery", scope="user")) == 2
    assert _embedding_ids(store) == ("a", "b")
    surface = GhostControlSurface.from_state_home(tmp_path)
    before = _history(surface, "observations")
    assert store.delete_scope("project", project=str(tmp_path / "a")) == 1
    assert _embedding_ids(store) == ("b",)
    assert [row["run_id"] for row in store.retrieve("Recovery", scope="user")] == ["b"]
    assert _history(surface, "observations") == before


@pytest.mark.parametrize("window", ["index", "query"])
def test_deletion_during_embedding_cannot_restore_vectors_or_return_retired_facts(tmp_path: Path, window: str) -> None:
    def delete() -> None:
        assert store.delete_scope("session", session_id="session") == 1

    embedder = _Embeddings()
    store = GhostObservationStore(tmp_path, embeddings=embedder)
    assert store.append_completed(
        run_id="old-run", session_id="session", mode="chat", user_text="Recovery check",
        assistant_text="Needs review", stop_reason="done",
    )
    if window == "query":
        assert store.retrieve("Recovery", session_id="session")
    embedder.before_encode = delete
    surface = GhostControlSurface.from_state_home(tmp_path)
    before = _history(surface, "observations")
    assert not store.retrieve("Recovery", session_id="session")
    assert not _embedding_ids(store)
    assert _history(surface, "observations") == before


@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("store", ["inbox", "hebbian", "sleep"])
def test_same_timestamp_delete_fresh_input_delete_remains_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, store: str,
) -> None:
    def now() -> str:
        return NOW

    monkeypatch.setattr("codey.ghost._common.now_iso_z", now)
    surface = _seed_history(tmp_path, scope)
    project = str(tmp_path / "project")
    assert surface.sleep is not None
    for cycle in range(3):
        if cycle:
            _seed(surface, project, scope=scope, run_id=f"fresh-{cycle}")
            surface.sleep.run_once(run_id=f"fresh-{cycle}", session_id="session", project=project)
        status, payload = surface.dispatch_action({
            "action": "delete_scope", "scope": scope, "project": project, "session_id": "session",
            "confirm": True, "delete_operation_id": f"same-clock-{cycle}",
        })
        assert status == 200, payload
        reopened = GhostControlSurface.from_state_home(tmp_path)
        if store == "inbox":
            assert reopened.inbox is not None
            with reopened.inbox.records.transaction():
                candidates = reopened.inbox._rebuild_candidates_from_events_unlocked()
                assert candidates is not None
                reopened.inbox._write_state(candidates)
        elif store == "hebbian":
            assert reopened.hebbian is not None and reopened.hebbian.rebuild_from_events()
        assert not _visible(reopened, store)
        assert reopened.sleep is not None
        if store == "sleep":
            assert reopened.sleep.delete_scope(scope, project=project, session_id="session")["reports"] == 0
