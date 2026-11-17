"""Frozen real node-cap transitions must survive unrelated scope deletion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.hebbian import MAX_GHOST_NODES, GhostHebbianStore, node_id_for_candidate
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.payload_types import object_fields
from tests.test_ghost_graph_tie_replay import _accepted, _digest
from tests.test_ghost_hebbian_control_boundaries import _disk_history

RETAINED_SCOPE: dict[str, str] = {"user": "project", "project": "session", "session": "user"}


def _timestamp(index: int) -> str:
    return (datetime(2026, 11, 18, tzinfo=UTC) + timedelta(seconds=index)).isoformat().replace("+00:00", "Z")


def _seed(home: Path, deleted_scope: str, pressure: str) -> tuple[GhostHebbianStore, set[str]]:
    assert MAX_GHOST_NODES == 500, "INCOMPLETE_FIXTURE: real production cap changed"
    inbox, store = GhostInboxStore(home), GhostHebbianStore(home)
    retained_scope = RETAINED_SCOPE[deleted_scope]
    retained_count = MAX_GHOST_NODES if pressure == "over_cap" else MAX_GHOST_NODES - 1
    retained: set[str] = set()
    for index in range(retained_count):
        # Let the real inbox window evict old accepted rows naturally. Every
        # input is reviewed and reinforced while it is still a current row.
        with patch("codey.ghost._common.now_iso_z", return_value=_timestamp(index)):
            candidate = _accepted(inbox, home, retained_scope, index)
            result = store.reinforce_candidate(candidate, reward=0.5)
        assert result.applied and result.node is not None, ("INCOMPLETE_FIXTURE: retained", index, result)
        assert result.node.weight == 0.125 and result.node.scope == retained_scope
        retained.add(result.node.id)
    assert len(store.list_nodes()) == retained_count and not store.list_edges()
    assert len(retained) == retained_count
    initial_history = _disk_history(store)
    with patch("codey.ghost._common.now_iso_z", return_value=_timestamp(MAX_GHOST_NODES)):
        strong = _accepted(inbox, home, deleted_scope, MAX_GHOST_NODES)
        result = store.reinforce_candidate(strong, reward=1.0)
    assert result.applied and result.node is not None, ("INCOMPLETE_FIXTURE: strong", result)
    assert result.node.weight == 0.25
    nodes = store.list_nodes()
    assert len(nodes) == MAX_GHOST_NODES and not store.list_edges()
    strong_id = node_id_for_candidate(strong)
    assert strong_id in {node.id for node in nodes}
    excluded = retained - {node.id for node in nodes}
    assert len(excluded) == (1 if pressure == "over_cap" else 0), "INCOMPLETE_FIXTURE: no real pruning"
    assert sum(node.scope == retained_scope for node in nodes) == MAX_GHOST_NODES - 1
    with patch("codey.ghost._common.now_iso_z", return_value=_timestamp(MAX_GHOST_NODES + 1)):
        removed = store.delete_scope(deleted_scope, project=str(home / "project"), session_id="s1")
    assert removed == {"nodes": 1, "edges": 0}, ("INCOMPLETE_FIXTURE: delete", removed)
    assert len(store.list_nodes()) == MAX_GHOST_NODES - 1
    assert all(node.scope == retained_scope for node in store.list_nodes())
    [control] = [event for event in store.graph.events() if event["type"] == "ghost_hebbian_scope_deleted"]
    payload = control["payload"]
    assert object_fields(payload)
    assert payload == {"scope": deleted_scope, "scope_ref": strong.scope_ref, "removed_nodes": 1, "removed_edges": 0}
    assert _disk_history(store)[:len(initial_history)] == initial_history, "producer rewrote original history"
    return store, excluded


@pytest.mark.parametrize("deleted_scope", tuple(RETAINED_SCOPE))
@pytest.mark.parametrize("restart", ("warm", "cold"))
@pytest.mark.parametrize("pressure", ("at_cap", "over_cap"))
def test_node_cap_pruning_remains_absent_after_other_scope_deletion_and_fixed_log_rebuild(
    tmp_path: Path, deleted_scope: str, restart: str, pressure: str,
) -> None:
    store, excluded = _seed(tmp_path, deleted_scope, pressure)
    before, history = store.graph.rows(), _disk_history(store)
    before_digest = _digest(before)
    if restart == "cold":
        store = GhostHebbianStore(tmp_path)
    accepted: list[bool] = []
    unchanged: list[bool] = []
    histories: list[bool] = []
    reappeared: list[list[str]] = []
    deleted_visible: list[int] = []
    node_counts: list[int] = []
    digests: list[str] = [before_digest]
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read wall clock")):
        for _ in range(2):
            accepted.append(store.rebuild_from_events())
            rows = store.graph.rows()
            digests.append(_digest(rows))
            unchanged.append(rows == before)
            histories.append(_disk_history(store) == history)
            nodes = store.list_nodes()
            reappeared.append(sorted(excluded & {node.id for node in nodes}))
            deleted_visible.append(sum(node.scope == deleted_scope for node in nodes))
            node_counts.append(len(nodes))
    measurement: dict[str, object] = {
        "deleted_scope": deleted_scope, "retained_scope": RETAINED_SCOPE[deleted_scope], "restart": restart,
        "pressure": pressure, "fixture_valid": True, "real_cap": MAX_GHOST_NODES, "accepted": accepted,
        "projection_unchanged": unchanged, "history_unchanged": histories, "excluded_count": len(excluded),
        "excluded_reappeared": reappeared, "deleted_scope_visible": deleted_visible,
        "before_node_count": len(before[0]), "replay_node_counts": node_counts, "digests": digests,
    }
    print("GRAPH_CAPACITY_SCOPE_CELL " + json.dumps(measurement, sort_keys=True))
    assert accepted == [True, True], "valid committed log was rejected"
    assert all(histories), "rebuild rewrote original audit history"
    assert deleted_visible == [0, 0], "deleted scope reappeared"
    assert all(unchanged), "rebuild changed live projection after unrelated scope deletion"
    assert reappeared == [[], []], "capacity-pruned identities reappeared without a new input"
