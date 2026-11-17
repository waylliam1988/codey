"""Expiry is a durable fact; readers share a horizon without deleting sources."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost import _common
from codey.ghost import continuity as continuity_module
from codey.ghost.continuity import (
    MAX_CONTINUITY_ITEMS,
    GhostContinuityItem,
    GhostContinuityResult,
    GhostContinuityStore,
    _item_event,
    _items_from_events,
    build_ghost_continuity,
    render_ghost_continuity,
)
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_sources import items_from_continuity
from codey.runtime.core import cancellation
from tests.test_ghost_continuity_expiry_boundary import AFTER, BASE, EXPIRY, _context, _history, _seed
from tests.test_ghost_maintenance_as_of import BAD_TIMES, ForbiddenWallClock


def _snapshot(store: GhostContinuityStore) -> dict[str, object]:
    with store.database.connection() as database:
        tables: dict[str, tuple[tuple[object, ...], ...]] = {}
        for table in ("memory_consumers", "ghost_replay_windows"):
            rows: Iterable[sqlite3.Row] = database.execute(f"SELECT * FROM {table} ORDER BY 1")
            values: list[tuple[object, ...]] = []
            for row in rows:
                fields: list[object] = []
                for index in range(len(row)):
                    field: object = row[index]
                    fields.append(field)
                values.append(tuple(fields))
            tables[table] = tuple(values)
    return {"rows": store.records.rows(), "history": _history(store), **tables}


def _expiry_event(**changes: object) -> dict[str, object]:
    event: dict[str, object] = {
        "schema_version": 1, "type": "ghost_continuity_items_expired", "ts": EXPIRY,
        "payload": {"item_ids": ["expired-context"]},
    }
    event.update(changes)
    return event


@pytest.mark.parametrize("scope", ("user", "project", "session"))
def test_expiry_is_atomic_idempotent_and_uses_explicit_time(tmp_path: Path, scope: str) -> None:
    ref = str(tmp_path / "project") if scope == "project" else "s1" if scope == "session" else ""
    store = _seed(tmp_path, scope, ref, "mixed")
    original = _history(store)
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock):
        first = store.sync_from_sources(maintenance_as_of="2026-04-01T08:00:00+08:00")
        after = _snapshot(store)
        second = store.sync_from_sources(maintenance_as_of=EXPIRY)
        assert _snapshot(store) == after
    assert clock.calls == 0
    assert first.ok and first.items_changed == 1 and first.total_items == 1
    assert second.ok and second.items_changed == 0 and second.total_items == 1
    assert _history(store)[:len(original)] == original
    events = store.records.events()
    expired = [event for event in events if event.get("type") == "ghost_continuity_items_expired"]
    assert expired == [_expiry_event()]
    assert [row["id"] for row in store.records.rows()] == ["live-context"]
    assert all(event["ts"] == EXPIRY for event in events[2:])


@pytest.mark.parametrize("scope", ("user", "project", "session"))
def test_expiry_allows_new_fact_from_same_source_then_rebuild(tmp_path: Path, scope: str) -> None:
    store = GhostContinuityStore(tmp_path)
    project = str(tmp_path / "project") if scope == "project" else ""
    session_id = "s1" if scope == "session" else ""
    def refresh(as_of: str) -> GhostContinuityResult:
        return store.sync_from_sources(
            user_focus_excerpt="Continue checking recovery boundaries", run_id="same-run",
            session_id=session_id, project=project, mode="chat", maintenance_as_of=as_of,
        )

    first = refresh(BASE)
    original = store.records.rows()
    assert first.ok and first.items_changed == 1
    refreshed = refresh(AFTER)
    rows = store.records.rows()
    assert refreshed.ok and refreshed.items_changed == 1 and refreshed.total_items == 1
    assert rows[0]["id"] == original[0]["id"] and rows[0]["source_ref"] == original[0]["source_ref"]
    assert rows[0]["expires_at"] != original[0]["expires_at"]
    events = store.records.events()
    assert refresh(AFTER).items_changed == 0
    assert store.records.events() == events
    with patch.object(_common, "now_iso_z", return_value="2027-01-01T00:00:00Z"):
        assert GhostContinuityStore(tmp_path).rebuild_from_events()
    assert store.records.rows() == rows
    assert store.list_items(project=project, session_id=session_id, maintenance_as_of=AFTER)


@pytest.mark.parametrize("limit", ("MAX_CONTINUITY_EVENTS", "MAX_CONTINUITY_EVENTS_BYTES"))
def test_expiry_checkpoint_keeps_history_and_rebuilds_without_resurrection(tmp_path: Path, limit: str) -> None:
    store = _seed(tmp_path, "user", "", "mixed")
    original = _history(store)
    clock = ForbiddenWallClock()
    with patch.object(continuity_module, limit, 1), patch.object(_common, "now_iso_z", clock):
        result = store.sync_from_sources(maintenance_as_of=AFTER)
    assert clock.calls == 0 and result.ok and result.items_changed == 1
    rows = store.records.rows()
    assert [row["id"] for row in rows] == ["live-context"]
    assert _history(store)[:len(original)] == original
    assert all(event["ts"] == AFTER for event in store.records.events())
    history = _history(store)
    assert GhostContinuityStore(tmp_path).rebuild_from_events()
    assert store.records.rows() == rows and _history(store) == history


@pytest.mark.parametrize("phase", ("append", "replace_rows", "checkpoint"))
def test_expiry_write_failures_roll_back_history_cursor_window_and_retry(tmp_path: Path, phase: str) -> None:
    store = _seed(tmp_path, "user", "", "mixed")
    before = _snapshot(store)

    def fail_checkpoint(items: Iterable[GhostContinuityItem], *, ts: str | None = None) -> None:
        store._rewrite_events_from_items(items, ts=ts)
        raise OSError("injected after checkpoint")

    target = store if phase == "checkpoint" else store.records
    attribute = "_compact_if_needed" if phase == "checkpoint" else phase
    replacement = fail_checkpoint if phase == "checkpoint" else None
    if replacement is not None:
        with patch.object(target, attribute, replacement):
            failed = store.sync_from_sources(maintenance_as_of=AFTER)
    else:
        with patch.object(target, attribute, side_effect=sqlite3.OperationalError("injected expiry write")):
            failed = store.sync_from_sources(maintenance_as_of=AFTER)
    assert not failed.ok and failed.warnings
    assert _snapshot(store) == before
    retry = store.sync_from_sources(maintenance_as_of=AFTER)
    assert retry.ok and retry.items_changed == 1
    assert [row["id"] for row in store.records.rows()] == ["live-context"]
    assert store.rebuild_from_events()
    assert [row["id"] for row in store.records.rows()] == ["live-context"]


BAD_IDENTITIES: tuple[object, ...] = (
    list[object](), "expired-context", [None], [42], [""], [" padded "], ["x" * 121],
    ["expired-context", "expired-context"], [str(index) for index in range(MAX_CONTINUITY_ITEMS + 1)],
)
BAD_EVENTS = (
    *(_expiry_event(ts=timestamp) for timestamp in (*BAD_TIMES, None, 42)),
    _expiry_event(schema_version=True), _expiry_event(schema_version=2),
    _expiry_event(payload=None), _expiry_event(payload={}),
    *(_expiry_event(payload={"item_ids": ids}) for ids in BAD_IDENTITIES),
    *(_expiry_event(type="ghost_continuity_synced", ts=timestamp) for timestamp in (*BAD_TIMES, None, 42)),
    _expiry_event(type="ghost_continuity_synced", schema_version=True),
    _expiry_event(type="ghost_continuity_synced", schema_version=2),
    _expiry_event(type="ghost_continuity_synced", payload=None),
)


@pytest.mark.parametrize("event", BAD_EVENTS)
def test_invalid_expiry_event_fails_rebuild_without_losing_valid_projection(tmp_path: Path, event: dict[str, object]) -> None:
    store = _seed(tmp_path, "user", "", "before")
    with store.records.transaction():
        store.records.append([event])
        store.records.mark_applied()
    before = _snapshot(store)
    with pytest.raises(ValueError):
        _items_from_events(store.records.events())
    assert not store.rebuild_from_events()
    assert _snapshot(store) == before


def test_recorded_expiry_only_removes_expired_ids_and_does_not_retire_sources() -> None:
    old = _context("expired-context", "user", "", EXPIRY)
    live = replace(old, expires_at="2028-01-01T00:00:00Z", updated_at=AFTER)
    events = [_item_event(old, action="upsert", ts=BASE), _expiry_event(),
              _item_event(live, action="upsert", ts=AFTER),
              _expiry_event(payload={"item_ids": [old.id, "unknown-context"]})]
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock):
        assert _items_from_events(events) == [live]
    assert clock.calls == 0


@pytest.mark.parametrize("operation", ("list", "build", "render"))
@pytest.mark.parametrize("bad_time", BAD_TIMES)
def test_invalid_reader_time_rejects_without_clock_fallback_or_mutation(tmp_path: Path, operation: str, bad_time: str) -> None:
    store = _seed(tmp_path, "user", "", "before")
    before = _snapshot(store)
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock), pytest.raises(ValueError, match="invalid Ghost timestamp"):
        if operation == "list":
            store.list_items(maintenance_as_of=bad_time)
        elif operation == "build":
            build_ghost_continuity(store, maintenance_as_of=bad_time)
        else:
            render_ghost_continuity([_context("expired-context", "user", "", EXPIRY)], maintenance_as_of=bad_time)
    assert clock.calls == 0 and _snapshot(store) == before


@dataclass
class OnceClock:
    calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("consumer read the clock more than once")
        return EXPIRY


@pytest.mark.parametrize("operation", ("list", "build", "render", "queue"))
def test_default_reader_and_real_queue_capture_time_once(tmp_path: Path, operation: str) -> None:
    store = _seed(tmp_path, "user", "", "before")
    queue = GhostWorkQueueStore(tmp_path)
    before = _snapshot(store)
    clock = OnceClock()
    with patch.object(_common, "now_iso_z", clock):
        if operation == "list":
            assert not store.list_items()
        elif operation == "build":
            assert not build_ghost_continuity(store).text
        elif operation == "render":
            assert not render_ghost_continuity([_context("expired-context", "user", "", EXPIRY)]).text
        else:
            assert queue.sync_from_sources(continuity_store=store).ok
            assert not queue.records.rows()
    assert clock.calls == 1 and _snapshot(store) == before


@pytest.mark.parametrize("operation", ("list", "build", "render", "convert"))
def test_explicit_reader_time_reaches_real_source_without_clock(tmp_path: Path, operation: str) -> None:
    store = _seed(tmp_path, "user", "", "mixed")
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock):
        if operation == "list":
            assert [item.id for item in store.list_items(maintenance_as_of=EXPIRY)] == ["live-context"]
        elif operation == "build":
            result = build_ghost_continuity(store, maintenance_as_of=EXPIRY)
            assert [item.id for item in result.selected_items] == ["live-context"] and not result.warnings
        elif operation == "render":
            result = render_ghost_continuity([
                _context("expired-context", "user", "", EXPIRY),
                _context("live-context", "user", "", "2028-01-01T00:00:00Z"),
            ], maintenance_as_of=EXPIRY)
            assert [item.id for item in result.selected_items] == ["live-context"]
        else:
            rows = items_from_continuity(store, session_id="", project="", now=EXPIRY)
            assert [row.source_ref for row in rows] == ["note:live-context"]
    assert clock.calls == 0


def test_expiry_cancellation_propagates_and_rolls_back(tmp_path: Path) -> None:
    store = _seed(tmp_path, "user", "", "mixed")
    before = _snapshot(store)
    with (
        patch.object(store.records, "replace_rows", side_effect=cancellation.TaskCancelled("cancel expiry")),
        pytest.raises(cancellation.TaskCancelled),
    ):
        store.sync_from_sources(maintenance_as_of=EXPIRY)
    assert _snapshot(store) == before


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("as_of", ("2026-03-31T23:59:59Z", EXPIRY, AFTER))
def test_legacy_sync_without_expiry_event_rebuilds_persisted_projection(tmp_path: Path, scope: str, as_of: str) -> None:
    """The old mixed-source path wrote upsert+sync but omitted expiry identities."""
    ref = str(tmp_path / "project") if scope == "project" else "s1" if scope == "session" else ""
    store = _seed(tmp_path, scope, ref, "before")
    live = _context("live-context", scope, ref, "2028-01-01T00:00:00Z")
    old = _context("expired-context", scope, ref, EXPIRY)
    expected = [old.to_payload(), live.to_payload()] if as_of < EXPIRY else [live.to_payload()]
    legacy_sync: dict[str, object] = {
        "schema_version": 1, "type": "ghost_continuity_synced", "ts": as_of,
        "payload": {"items_seen": 1, "items_changed": 1, "items_total": len(expected)},
    }
    with store.records.transaction():
        store.records.append([_item_event(live, action="upsert", ts=as_of), legacy_sync])
        store.records.replace_rows(expected)
    history = _history(store)
    # A no-op refresh cannot discover the old item missing only from projection.
    result = store.sync_from_sources(maintenance_as_of=as_of)
    assert result.ok and result.items_changed == 0 and result.total_items == len(expected)
    assert _history(store) == history
    forbidden = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", forbidden):
        assert [item.to_payload() for item in _items_from_events(store.records.events())] == expected
    assert forbidden.calls == 0
    for clock in (BASE, "2029-01-01T00:00:00Z"):
        with patch.object(_common, "now_iso_z", return_value=clock):
            assert GhostContinuityStore(tmp_path).rebuild_from_events()
        assert store.records.rows() == expected, "upgrade replay revived an item removed by recorded legacy sync"
        assert _history(store) == history
