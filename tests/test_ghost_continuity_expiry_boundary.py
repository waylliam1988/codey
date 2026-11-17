"""Expired continuity must not survive maintenance/replay or create fresh work."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost import _common
from codey.ghost.continuity import GhostContinuityItem, GhostContinuityStore, _item_event, build_ghost_continuity
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore
from tests.test_ghost_sleep_clock_boundary import _seed_hebbian

BASE = "2026-01-01T00:00:00Z"
EXPIRY = "2026-04-01T00:00:00Z"
AFTER = "2026-04-02T00:00:00Z"
REBUILD_CLOCKS = ("2026-03-01T00:00:00Z", "2027-01-01T00:00:00Z")
VARIANTS = ("before", "at", "after", "mixed", "new_source", "reader_only")


def _context(item_id: str, scope: str, scope_ref: str, expires_at: str) -> GhostContinuityItem:
    return GhostContinuityItem(
        id=item_id, kind="open_question", scope=scope, scope_ref=scope_ref,
        text=f"Which {item_id} recovery step needs review?", source="research_note", source_ref=f"note:{item_id}",
        weight=0.8, confidence=0.9, created_at=BASE, updated_at=BASE, expires_at=expires_at,
    )


def _seed(home: Path, scope: str, scope_ref: str, variant: str) -> GhostContinuityStore:
    store = GhostContinuityStore(home)
    items = [_context("expired-context", scope, scope_ref, EXPIRY)]
    if variant == "mixed":
        items.append(_context("live-context", scope, scope_ref, "2028-01-01T00:00:00Z"))
    events = [{**_item_event(item, action="upsert", ts=BASE), "event_id": f"expiry-seed-{index}"}
              for index, item in enumerate(items)]
    with store.records.transaction():
        store.records.append(events)
        store.records.replace_rows(item.to_payload() for item in items)
    assert store.records.rows() == [item.to_payload() for item in items]
    return store


def _history(store: GhostContinuityStore) -> tuple[tuple[object, ...], ...]:
    with store.database.connection() as database:
        rows: Iterable[sqlite3.Row] = database.execute(
            "SELECT sequence,event_id,payload FROM memory_events WHERE collection='continuity' ORDER BY sequence",
        )
        values: list[tuple[object, ...]] = []
        for row in rows:
            sequence: object = row[0]
            event_id: object = row[1]
            payload: object = row[2]
            values.append((sequence, event_id, payload))
        return tuple(values)


def _digest(rows: object) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ExpiryMeasurement:
    expected_records: int
    expected_visible: int
    records: tuple[dict[str, object], ...]
    reader_ids: tuple[str, ...]
    work_source_refs: tuple[str, ...]
    rendered_expired: bool
    reported_total: int | None
    rebuilt: tuple[tuple[dict[str, object], ...], ...]

    def payload(self) -> dict[str, object]:
        return {
            "expected_records": self.expected_records, "expected_visible": self.expected_visible,
            "record_ids": [row["id"] for row in self.records], "record_digest": _digest(self.records),
            "reader_ids": self.reader_ids, "work_source_refs": self.work_source_refs,
            "rendered_expired": self.rendered_expired, "reported_total": self.reported_total,
            "rebuilt_ids": [[row["id"] for row in rows] for rows in self.rebuilt],
            "rebuilt_digests": [_digest(rows) for rows in self.rebuilt],
        }


def _measure(home: Path, scope: str, scope_ref: str, variant: str) -> ExpiryMeasurement:
    as_of = "2026-03-31T23:59:59Z" if variant == "before" else EXPIRY if variant == "at" else AFTER
    store = _seed(home, scope, scope_ref, variant)
    original_history = _history(store)
    assert original_history
    hebbian: GhostHebbianStore | None = _seed_hebbian(home, scope, scope_ref) if variant == "new_source" else None
    project = scope_ref if scope == "project" else ""
    session_id = scope_ref if scope == "session" else ""
    reported_total: int | None = None
    queue = GhostWorkQueueStore(home)
    with patch.object(_common, "now_iso_z", return_value=as_of):
        if variant != "reader_only":
            report = GhostSleepStore(home).run_once(
                continuity_store=store, hebbian_store=hebbian, project=project, session_id=session_id,
                maintenance_as_of=as_of,
            )
            step = next(step for step in report.steps if step.name == "continuity_refresh")
            assert step.ok and not step.warnings, "fixture must reach successful ordinary maintenance"
            reported_total = step.counts["total_items"]
        records = tuple(GhostContinuityStore(home).records.rows())
        reader = GhostContinuityStore(home).list_items(scope=scope, project=project, session_id=session_id)
        rendered = build_ghost_continuity(GhostContinuityStore(home), project=project, session_id=session_id)
        sync = queue.sync_from_sources(continuity_store=GhostContinuityStore(home), project=project, session_id=session_id)
        assert sync.ok and not sync.warnings
        work = queue.list_items(project=project, session_id=session_id)
    assert _history(store)[:len(original_history)] == original_history, "expiry must retain original audit history"
    assert tuple(store.records.rows()) == records, "consumer reads must not rewrite continuity state"
    rebuilt: list[tuple[dict[str, object], ...]] = []
    history_before_rebuild = _history(store)
    for clock in REBUILD_CLOCKS:
        reopened = GhostContinuityStore(home)
        with patch.object(_common, "now_iso_z", return_value=clock):
            assert reopened.rebuild_from_events()
        rebuilt.append(tuple(GhostContinuityStore(home).records.rows()))
        assert _history(store) == history_before_rebuild, "rebuild must not rewrite raw history"
    expected_records = 1 if variant in {"before", "mixed", "new_source", "reader_only"} else 0
    expected_visible = 1 if variant in {"before", "mixed", "new_source"} else 0
    return ExpiryMeasurement(
        expected_records, expected_visible, records, tuple(item.id for item in reader),
        tuple(item.source_ref for item in work), "expired-context" in rendered.text, reported_total, tuple(rebuilt),
    )


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", VARIANTS)
def test_expiry_persistence_replay_and_real_work_consumer_agree(tmp_path: Path, scope: str, variant: str) -> None:
    scope_ref = str((tmp_path / "project").resolve()) if scope == "project" else "s1" if scope == "session" else ""
    result = _measure(tmp_path, scope, scope_ref, variant)
    measurement: dict[str, object] = {"scope": scope, "variant": variant, **result.payload()}
    print("CONTINUITY_EXPIRY_CELL " + json.dumps(measurement, sort_keys=True))
    assert len(result.records) == result.expected_records, "successful maintenance left expired persisted records"
    if result.reported_total is not None:
        assert result.reported_total == len(result.records), "maintenance report disagreed with durable projection"
    assert len(result.reader_ids) == result.expected_visible, "ordinary reader returned expired continuity"
    assert result.rendered_expired == (variant == "before"), "renderer expiry control failed"
    assert ("note:expired-context" in result.work_source_refs) == (variant == "before"), "expired source created fresh work"
    assert ("note:live-context" in result.work_source_refs) == (variant == "mixed"), "live-source queue control failed"
    assert all(rows == result.records for rows in result.rebuilt), "rebuild resurrected state dropped by maintenance"
