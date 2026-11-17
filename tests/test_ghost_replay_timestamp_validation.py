"""Bad historical time is rejected atomically; replay never consults a clock."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Self

import pytest

from codey.ghost import graph_primitives
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import (
    AffinityReplayState,
    _decay_applied_event,
    _scope_deleted_event,
    _snapshot_event,
    apply_affinity_event,
    replay_affinity_events,
)
from codey.ghost.affinity_model import AffinityNode
from codey.ghost.payload_types import object_fields
from codey.ghost.sleep import GhostSleepStore
from tests.test_ghost_replay_clock_boundary import AS_OF, BASE, _events

BAD_TIMES: tuple[object, ...] = (
    None, False, 7, 1.5, "", " ", "not-a-date", "2026-02-30T00:00:00Z", "2026-01-01T00:00:00+25:00",
)
EVENT_KINDS = ("node", "edge", "scope_delete", "decay", "snapshot")


class _ClockForbidden(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        raise AssertionError("historical time must not consult the wall clock")


def _event(kind: str) -> dict[str, object]:
    initial, _suffix = _events("user", "", "valid")
    nodes, edges = replay_affinity_events(initial)
    events: dict[str, dict[str, object]] = {
        "node": initial[0],
        "edge": initial[2],
        "scope_delete": _scope_deleted_event("user", "", removed_nodes=2, removed_edges=1, ts=BASE),
        "decay": _decay_applied_event(
            removed_nodes=0, removed_edges=0, decayed_nodes=2, decayed_edges=1, min_interval_seconds=0, ts=AS_OF,
        ),
        "snapshot": _snapshot_event(nodes, edges, ts=BASE, reason="timestamp_validation_fixture"),
    }
    return events[kind]


@pytest.mark.parametrize("bad_time", BAD_TIMES)
@pytest.mark.parametrize("kind", EVENT_KINDS)
def test_each_affinity_envelope_rejects_bad_time_before_any_state_mutation(
    monkeypatch: pytest.MonkeyPatch, kind: str, bad_time: object,
) -> None:
    event = _event(kind)
    event["ts"] = bad_time
    state: AffinityReplayState = {}
    monkeypatch.setattr(graph_primitives, "datetime", _ClockForbidden)
    with pytest.raises(ValueError, match="invalid affinity event envelope"):
        apply_affinity_event(state, event)
    assert state == {}, "validation precedes even creation of empty state dictionaries"


@pytest.mark.parametrize("field", ("created_at", "updated_at", "last_reinforced_at", "last_decayed_at"))
@pytest.mark.parametrize("target", ("node", "edge"))
def test_snapshot_rejects_bad_recorded_time_before_replacing_valid_state(field: str, target: str) -> None:
    initial, _suffix = _events("user", "", "valid")
    nodes, edges = replay_affinity_events(initial)
    snapshot = _snapshot_event(nodes, edges, ts=BASE, reason="bad_row_time")
    rows = [row.to_payload() for row in (nodes if target == "node" else edges)]
    rows[0][field] = "not-a-date"
    snapshot["nodes" if target == "node" else "edges"] = rows
    state: AffinityReplayState = {"nodes": {node.id: node for node in nodes}, "edges": {edge.id: edge for edge in edges}}
    before = deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, snapshot)
    assert state == before


def test_legacy_snapshot_with_optional_empty_times_keeps_real_decay_anchor() -> None:
    initial, _suffix = _events("user", "", "valid")
    nodes, edges = replay_affinity_events(initial)
    legacy_nodes = [replace(node, created_at="", last_decayed_at="") for node in nodes]
    legacy_edges = [replace(edge, created_at="", last_decayed_at="") for edge in edges]
    snapshot = _snapshot_event(legacy_nodes, legacy_edges, ts=BASE, reason="legacy_optional_times")
    after_nodes, after_edges = replay_affinity_events([snapshot])
    assert after_nodes == legacy_nodes
    assert after_edges == legacy_edges


def test_snapshot_without_any_decay_anchor_is_rejected() -> None:
    initial, _suffix = _events("user", "", "valid")
    nodes, edges = replay_affinity_events(initial)
    missing = [replace(node, updated_at="", last_reinforced_at="", last_decayed_at="") for node in nodes]
    with pytest.raises(ValueError):
        replay_affinity_events([_snapshot_event(missing, edges, ts=BASE, reason="missing_anchor")])


@pytest.mark.parametrize("timestamp", (BASE, "2026-01-01T00:00:00", "2026-01-01T08:00:00+08:00"))
def test_recorded_time_parsing_and_decay_are_independent_of_current_clock(
    monkeypatch: pytest.MonkeyPatch, timestamp: str,
) -> None:
    monkeypatch.setattr(graph_primitives, "datetime", _ClockForbidden)
    assert graph_primitives.parse_ts(timestamp) == datetime(2026, 1, 1, tzinfo=UTC)
    assert graph_primitives.decayed_by_half_life(1.0, timestamp, AS_OF, 90.0) == pytest.approx(0.5)
    assert graph_primitives.any_decay_due([timestamp], now=AS_OF, min_interval_seconds=3600)


@pytest.mark.parametrize("bad_time", BAD_TIMES)
def test_shared_time_parser_rejects_invalid_input_without_reading_clock(
    monkeypatch: pytest.MonkeyPatch, bad_time: object,
) -> None:
    monkeypatch.setattr(graph_primitives, "datetime", _ClockForbidden)
    with pytest.raises(ValueError, match="invalid Ghost timestamp"):
        graph_primitives.parse_ts(bad_time)


@pytest.mark.parametrize("bad_basis", (True, False))
def test_shared_math_propagates_bad_time_instead_of_unchanged_weight_or_skip(bad_basis: bool) -> None:
    basis = "not-a-date" if bad_basis else BASE
    now = AS_OF if bad_basis else "not-a-date"
    with pytest.raises(ValueError):
        graph_primitives.decayed_by_half_life(0.8, basis, now, 90.0)
    with pytest.raises(ValueError):
        graph_primitives.any_decay_due([basis], now=now, min_interval_seconds=3600)


def test_random_event_ids_do_not_change_replayed_projection() -> None:
    initial, suffix = _events("user", "", "valid")
    events = [*initial, *suffix]
    renamed = [{**event, "event_id": f"independent-event-{index}"} for index, event in enumerate(events)]
    assert replay_affinity_events(events) == replay_affinity_events(renamed)


def test_ordinary_sleep_reports_corrupt_replay_and_preserves_last_valid_projection(tmp_path: Path) -> None:
    initial, suffix = _events("user", "", "decay")
    nodes, edges = replay_affinity_events(initial)
    store = GhostAffinityStore(tmp_path)
    with store.graph.transaction():
        store.graph.append((*initial, *suffix))
        store.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    before = store.graph.rows()
    events = store.graph.events()
    report = GhostSleepStore(tmp_path).run_once(affinity_store=store)
    steps = {step.name: step for step in report.steps}
    assert not steps["affinity_sync"].ok
    assert not steps["affinity_decay"].ok
    assert steps["affinity_decay"].skipped_reason == "step_error"
    assert "affinity_decay:ValueError" in report.warnings
    assert store.graph.rows() == before
    assert store.graph.events() == events
    def node_id(node: AffinityNode) -> str:
        return node.id

    assert GhostAffinityStore(tmp_path).list_nodes() == tuple(sorted(nodes, key=node_id))
    state = GhostSleepStore(tmp_path).export_state()["sleep"]
    assert object_fields(state)
    assert state["report"] == report.to_payload()
