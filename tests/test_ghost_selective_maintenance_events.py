"""Strict maintenance selection events and failure-preserving actual rebuilds."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.ghost.affinity_events import _maintenance_applied_event, replay_affinity_events
from codey.ghost.affinity_model import MAX_AFFINITY_EDGES, MAX_AFFINITY_NODES
from codey.ghost.continuity import (
    MAX_CONTINUITY_ITEMS,
    _items_from_events,
    _maintenance_membership_event,
)
from tests.test_ghost_selective_maintenance import HORIZON, _fixture

FAULTS = (
    "schema_bool", "schema_wrong", "time", "event_id", "extra", "missing_field", "not_list",
    "duplicate", "identity_type", "identity_empty", "identity_space", "identity_long", "missing_reference", "too_many",
)


def _identities(rows: list[dict[str, object]]) -> list[str]:
    identities: list[str] = []
    for row in rows:
        identity = row.get("id")
        assert isinstance(identity, str)
        identities.append(identity)
    return identities


def _event(domain: str, identities: list[str]) -> dict[str, object]:
    if domain == "affinity":
        return _maintenance_applied_event(identities, (), ts=HORIZON)
    return _maintenance_membership_event(identities, ts=HORIZON)


def _bad_event(domain: str, fault: str, identity: str) -> dict[str, object]:
    event = _event(domain, [identity])
    if fault == "schema_bool":
        event["schema_version"] = True
    elif fault == "schema_wrong":
        event["schema_version"] = 999
    elif fault == "time":
        event["ts"] = "not-a-time"
    elif fault == "event_id":
        event["event_id"] = 123
    elif fault == "extra":
        event["unrecognized"] = True
    elif fault == "missing_field":
        del event["node_ids" if domain == "affinity" else "payload"]
    else:
        limit = MAX_AFFINITY_NODES if domain == "affinity" else MAX_CONTINUITY_ITEMS
        values: object
        if fault == "not_list":
            values = (identity,)
        elif fault == "duplicate":
            values = [identity, identity]
        elif fault == "identity_type":
            values = [True]
        elif fault == "identity_empty":
            values = [""]
        elif fault == "identity_space":
            values = [f" {identity}"]
        elif fault == "identity_long":
            values = ["x" * 121]
        elif fault == "missing_reference":
            values = ["missing-record"]
        elif fault == "too_many":
            values = [f"record-{index}" for index in range(limit + 1)]
        else:
            raise ValueError(fault)
        if domain == "affinity":
            event["node_ids"] = values
        else:
            event["payload"] = {"item_ids": values}
    return event


@pytest.mark.parametrize("domain", ("affinity", "continuity"))
@pytest.mark.parametrize("fault", FAULTS)
def test_invalid_selection_event_is_rejected(tmp_path: Path, domain: str, fault: str) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    if domain == "affinity":
        rows, _edges = stores.affinity.graph.rows()
        event = _bad_event(domain, fault, _identities(rows)[0])
        with pytest.raises(ValueError):
            replay_affinity_events((*stores.affinity.graph.events(), event))
    else:
        event = _bad_event(domain, fault, _identities(stores.continuity.records.rows())[0])
        with pytest.raises(ValueError):
            _items_from_events((*stores.continuity.records.events(), event))


@pytest.mark.parametrize("fault", ("not_list", "duplicate", "identity_type", "missing_reference", "too_many"))
def test_invalid_selected_edge_is_rejected(tmp_path: Path, fault: str) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    _nodes, edges = stores.affinity.graph.rows()
    identity = _identities(edges)[0]
    event = _maintenance_applied_event((), [identity], ts=HORIZON)
    if fault == "not_list":
        event["edge_ids"] = identity
    elif fault == "duplicate":
        event["edge_ids"] = [identity, identity]
    elif fault == "identity_type":
        event["edge_ids"] = [False]
    elif fault == "missing_reference":
        event["edge_ids"] = ["missing-edge"]
    else:
        event["edge_ids"] = [f"edge-{index}" for index in range(MAX_AFFINITY_EDGES + 1)]
    with pytest.raises(ValueError):
        replay_affinity_events((*stores.affinity.graph.events(), event))


@pytest.mark.parametrize("domain", ("affinity", "continuity"))
@pytest.mark.parametrize("empty", (False, True))
def test_valid_selection_replay_is_deterministic(tmp_path: Path, domain: str, empty: bool) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    if domain == "affinity":
        rows, edges = stores.affinity.graph.rows()
        selected = [] if empty else [_identities(rows)[0]]
        events = (*stores.affinity.graph.events(), _event(domain, selected))
        nodes, replay_edges = replay_affinity_events(events)
        assert replay_affinity_events(events) == (nodes, replay_edges)
        assert sorted((edge.to_payload() for edge in replay_edges), key=_row_id) == sorted(edges, key=_row_id)
        expected_changes = 0 if empty else 1
        assert sum(node.to_payload() not in rows for node in nodes) == expected_changes
    else:
        selected = [] if empty else [_identities(stores.continuity.records.rows())[0]]
        events = (*stores.continuity.records.events(), _event(domain, selected))
        items = _items_from_events(events)
        assert _items_from_events(events) == items and {item.id for item in items} == set(selected)


def _row_id(row: dict[str, object]) -> str:
    identity = row.get("id")
    assert isinstance(identity, str)
    return identity


@pytest.mark.parametrize("domain", ("affinity", "continuity"))
def test_bad_persisted_selection_rebuild_preserves_previous_projection(tmp_path: Path, domain: str) -> None:
    stores, _target, _peer = _fixture(tmp_path)
    bad = _event(domain, ["missing-record"])
    if domain == "affinity":
        nodes, edges = stores.affinity.graph.rows()
        with stores.affinity.graph.transaction():
            stores.affinity.graph.append([bad])
            stores.affinity.graph.replace_rows(nodes, edges)
        with pytest.raises(ValueError):
            stores.affinity.rebuild_from_events()
        assert stores.affinity.graph.rows() == (nodes, edges)
    else:
        rows = stores.continuity.records.rows()
        with stores.continuity.records.transaction():
            stores.continuity.records.append([bad])
            stores.continuity.records.replace_rows(rows)
        assert not stores.continuity.rebuild_from_events()
        assert stores.continuity.records.rows() == rows
