"""Explicit logical time reaches real Sleep consumers and survives audit/reopen."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost import _common
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import replay_affinity_events
from codey.ghost.affinity_model import GhostAffinitySyncResult
from codey.ghost.continuity import GhostContinuityResult, GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.payload_types import object_fields
from codey.ghost.sleep import GhostSleepReport, GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import GhostWorkSyncResult
from tests.test_ghost_replay_clock_boundary import _events
from tests.test_ghost_sleep_clock_boundary import (
    BACKWARD,
    FORWARD,
    HORIZON,
    _fixed_event_ids,
    _seed_hebbian,
    _seed_queue,
)

OPERATIONS = ("hebbian_decay", "affinity_decay", "affinity_sync", "continuity_refresh", "queue_reconcile", "queue_read")
BAD_TIMES = ("", "not-a-date", "2026-02-30T00:00:00Z", "99999-01-01T00:00:00Z", "2026-01-01T00:00:00+99:00")


@dataclass(frozen=True)
class Stores:
    hebbian: GhostHebbianStore
    affinity: GhostAffinityStore
    continuity: GhostContinuityStore
    queue: GhostWorkQueueStore
    sleep: GhostSleepStore


def _stores(home: Path, scope: str = "user", scope_ref: str = "") -> Stores:
    hebbian = _seed_hebbian(home, scope, scope_ref)
    affinity = GhostAffinityStore(home)
    initial, _suffix = _events(scope, scope_ref, "valid")
    initial = _fixed_event_ids(initial)
    nodes, edges = replay_affinity_events(initial)
    with affinity.graph.transaction():
        affinity.graph.append(initial)
        affinity.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    return Stores(hebbian, affinity, GhostContinuityStore(home), _seed_queue(home, scope, scope_ref, source_only=False), GhostSleepStore(home))


def _state(stores: Stores, *, with_events: bool = True) -> dict[str, object]:
    result: dict[str, object] = {
        "hebbian": stores.hebbian.graph.rows(), "affinity": stores.affinity.graph.rows(),
        "continuity": stores.continuity.records.rows(), "queue": stores.queue.records.rows(),
    }
    if with_events:
        result.update({
            "hebbian_events": stores.hebbian.graph.events(), "affinity_events": stores.affinity.graph.events(),
            "continuity_events": stores.continuity.records.events(), "queue_events": stores.queue.records.events(),
            "sleep_events": stores.sleep.records.events(), "sleep": stores.sleep.records.rows(),
        })
    return result


def _operation(stores: Stores, operation: str, as_of: str) -> object:
    if operation == "hebbian_decay":
        return stores.hebbian.decay(maintenance_as_of=as_of)
    if operation == "affinity_decay":
        return stores.affinity.decay(maintenance_as_of=as_of)
    if operation == "affinity_sync":
        return stores.affinity.sync_from_sources(hebbian_store=stores.hebbian, work_queue_store=stores.queue, maintenance_as_of=as_of)
    if operation == "continuity_refresh":
        return stores.continuity.sync_from_sources(hebbian_store=stores.hebbian, mode="sleep", maintenance_as_of=as_of)
    if operation == "queue_reconcile":
        return stores.queue.reconcile_stale_claims(maintenance_as_of=as_of)
    if operation == "queue_read":
        return stores.queue.list_items(maintenance_as_of=as_of)
    if operation == "sleep":
        return stores.sleep.run_once(
            hebbian_store=stores.hebbian, affinity_store=stores.affinity, continuity_store=stores.continuity,
            work_queue_store=stores.queue, maintenance_as_of=as_of,
        )
    raise ValueError("unknown test operation")


@dataclass
class ForbiddenWallClock:
    calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        raise AssertionError("explicit maintenance consumer read the wall clock")


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("operation", OPERATIONS)
def test_explicit_consumer_uses_no_hidden_clock(tmp_path: Path, scope: str, operation: str) -> None:
    scope_ref = str((tmp_path / "project").resolve()) if scope == "project" else "s1" if scope == "session" else ""
    stores = _stores(tmp_path, scope, scope_ref)
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock):
        result = _operation(stores, operation, HORIZON)
    assert clock.calls == 0, "a source reader must not swallow a hidden clock failure"
    if isinstance(result, (GhostAffinitySyncResult, GhostContinuityResult, GhostWorkSyncResult)):
        assert result.ok and not result.warnings
        changed = result.nodes_changed if isinstance(result, GhostAffinitySyncResult) else result.items_changed
        assert changed > 0
    elif operation in {"hebbian_decay", "affinity_decay"}:
        assert object_fields(result) and result["decayed_nodes"] != 0
    else:
        assert result == stores.queue.list_items(maintenance_as_of=HORIZON)
    if operation == "hebbian_decay":
        assert stores.hebbian.list_nodes()[0].weight == 0.4
    if operation == "queue_reconcile":
        rows = stores.queue.list_items(maintenance_as_of=HORIZON)
        assert sorted(item.status for item in rows) == ["queued", "running"]


@pytest.mark.parametrize("operation", (*OPERATIONS, "sleep"))
@pytest.mark.parametrize("bad_time", BAD_TIMES)
def test_invalid_explicit_time_never_mutates_or_falls_back(tmp_path: Path, operation: str, bad_time: str) -> None:
    stores = _stores(tmp_path)
    before = _state(stores)
    with patch.object(_common, "now_iso_z", return_value=FORWARD):
        if operation in {"hebbian_decay", "affinity_decay", "queue_read", "sleep"}:
            with pytest.raises(ValueError, match="invalid Ghost timestamp"):
                _operation(stores, operation, bad_time)
        else:
            result = _operation(stores, operation, bad_time)
            assert isinstance(result, (GhostAffinitySyncResult, GhostContinuityResult, GhostWorkSyncResult))
            assert not result.ok
    assert _state(stores) == before


@pytest.mark.parametrize("as_of", (HORIZON, "2026-04-01T08:00:00+08:00", "2026-04-01T00:00:00"))
def test_explicit_sleep_time_is_normalized_and_reopened_independently_of_audit_clock(tmp_path: Path, as_of: str) -> None:
    results: list[dict[str, object]] = []
    for index, audit_clock in enumerate((BACKWARD, HORIZON, FORWARD)):
        home = tmp_path / str(index)
        stores = _stores(home)
        with patch.object(_common, "now_iso_z", return_value=audit_clock):
            report = _operation(stores, "sleep", as_of)
        assert isinstance(report, GhostSleepReport)
        assert report.started_at == audit_clock and report.finished_at == audit_clock
        assert report.maintenance_as_of == HORIZON
        assert all(step.ok for step in report.steps) and not report.warnings
        persisted = GhostSleepStore(home).export_state()["sleep"]
        assert object_fields(persisted)
        reopened = GhostSleepReport.from_payload(persisted["report"])
        assert reopened == report
        results.append({
            "state": _state(stores, with_events=False),
            "steps": [replace(step, duration_ms=0) for step in report.steps],
        })
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("operation", ("hebbian_decay", "affinity_decay"))
def test_same_logical_time_respects_minimum_interval_without_new_events(tmp_path: Path, operation: str) -> None:
    stores = _stores(tmp_path)
    _operation(stores, operation, HORIZON)
    before = _state(stores)
    clock = ForbiddenWallClock()
    with patch.object(_common, "now_iso_z", clock):
        if operation == "hebbian_decay":
            result = stores.hebbian.decay(min_interval_seconds=3600, maintenance_as_of=HORIZON)
        else:
            result = stores.affinity.decay(min_interval_seconds=3600, maintenance_as_of=HORIZON)
    assert result["skipped_reason"] == "min_interval"
    assert clock.calls == 0
    assert _state(stores) == before


@pytest.mark.parametrize("operation", ("continuity_refresh", "queue_reconcile", "affinity_sync"))
def test_write_failure_at_explicit_time_rolls_back_and_can_retry(tmp_path: Path, operation: str) -> None:
    stores = _stores(tmp_path)
    before = _state(stores)
    owner = stores.continuity.records if operation == "continuity_refresh" else stores.queue.records if operation == "queue_reconcile" else stores.affinity.graph
    with patch.object(owner, "replace_rows", side_effect=OSError("injected projection write failure")):
        result = _operation(stores, operation, HORIZON)
    assert isinstance(result, (GhostAffinitySyncResult, GhostContinuityResult, GhostWorkSyncResult))
    assert not result.ok
    assert _state(stores) == before
    retried = _operation(stores, operation, HORIZON)
    assert isinstance(retried, (GhostAffinitySyncResult, GhostContinuityResult, GhostWorkSyncResult))
    assert retried.ok


def test_cancelled_cycle_records_logical_horizon_without_running_maintenance(tmp_path: Path) -> None:
    stores = _stores(tmp_path)
    before = _state(stores, with_events=False)
    report = stores.sleep.run_once(
        hebbian_store=stores.hebbian, affinity_store=stores.affinity, continuity_store=stores.continuity,
        work_queue_store=stores.queue, maintenance_as_of=HORIZON, should_cancel=lambda: True,
    )
    assert report.cancelled and report.maintenance_as_of == HORIZON
    assert [step.name for step in report.steps] == ["report"]
    assert _state(stores, with_events=False) == before
    assert GhostSleepReport.from_payload(report.to_payload()) == report


def test_legacy_report_without_logical_time_remains_readable_without_guessing(tmp_path: Path) -> None:
    report = GhostSleepStore(tmp_path).run_once(maintenance_as_of=HORIZON)
    legacy = report.to_payload()
    del legacy["maintenance_as_of"]
    reopened = GhostSleepReport.from_payload(legacy)
    assert reopened is not None and reopened.maintenance_as_of == ""
    assert reopened == replace(report, maintenance_as_of="")
