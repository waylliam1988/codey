"""Recorded pruning is bounded, atomic, replayable and distinct from deletion."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.payload_types import object_fields
from tests.test_ghost_graph_capacity_scope_replay import RETAINED_SCOPE
from tests.test_ghost_graph_capacity_scope_replay import _seed as _seed_capacity
from tests.test_ghost_graph_tie_replay import NOW, _accepted, _candidate_edge_key
from tests.test_ghost_graph_tie_replay import _seed as _seed_degree
from tests.test_ghost_hebbian_control_boundaries import _assert_rejected, _change, _disk_history, _payload

PRUNED = "ghost_hebbian_projection_pruned"
BAD_PAYLOADS = (
    "null", "scalar", "missing_nodes", "missing_edges", "scalar_nodes", "scalar_edges", "extra_key",
    "bad_node", "empty_node", "whitespace_node", "duplicate_node", "unknown_node", "scalar_edge",
    "short_edge", "long_edge", "bad_source", "empty_target", "bad_relation", "duplicate_edge", "unknown_edge", "empty",
)


def _edge_key_from_payload(payload: dict[str, object]) -> list[str]:
    raw = payload["edge_keys"]
    assert isinstance(raw, list)
    keys: Sequence[object] = raw
    [first] = keys
    assert isinstance(first, list)
    parts: Sequence[object] = first
    source, target, relation = parts
    assert isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)
    return [source, target, relation]


def _invalid_payload(store: GhostHebbianStore, variant: str) -> object:
    payload = _payload(store, "projection_pruned")
    source, target, relation = _edge_key_from_payload(payload)
    if variant == "null":
        return None
    if variant == "scalar":
        return "invalid"
    if variant == "missing_nodes":
        payload.pop("node_ids")
    elif variant == "missing_edges":
        payload.pop("edge_keys")
    elif variant == "extra_key":
        payload["unexpected"] = 0
    elif variant in {"scalar_nodes", "scalar_edges"}:
        payload["node_ids" if variant == "scalar_nodes" else "edge_keys"] = 1
    elif variant in {"bad_node", "empty_node", "whitespace_node", "duplicate_node", "unknown_node"}:
        bad_nodes: dict[str, object] = {
            "bad_node": [True], "empty_node": [""], "whitespace_node": [" "],
            "duplicate_node": ["absent", "absent"], "unknown_node": ["absent"],
        }
        payload["node_ids"] = bad_nodes[variant]
    elif variant == "empty":
        no_nodes: list[str] = []
        no_edges: list[list[str]] = []
        payload = {"node_ids": no_nodes, "edge_keys": no_edges}
    else:
        bad_edges: dict[str, object] = {
            "scalar_edge": [1], "short_edge": [[source, target]], "long_edge": [[source, target, relation, "extra"]],
            "bad_source": [[False, target, relation]], "empty_target": [[source, "", relation]],
            "bad_relation": [[source, target, "unknown"]],
            "duplicate_edge": [[source, target, relation], [source, target, relation]],
            "unknown_edge": [["absent", target, relation]],
        }
        payload["edge_keys"] = bad_edges[variant]
    return payload


@pytest.mark.parametrize("variant", BAD_PAYLOADS)
def test_malformed_pruning_rejects_without_replacing_projection_or_original_history(tmp_path: Path, variant: str) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _ = _seed_degree(tmp_path, "user", tied=True)
    _change(store, "projection_pruned", _invalid_payload(store, variant))
    _assert_rejected(store, tmp_path)


def test_no_pruning_appends_only_real_changes_and_duplicate_is_a_noop(tmp_path: Path) -> None:
    inbox, store = GhostInboxStore(tmp_path), GhostHebbianStore(tmp_path)
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        left, right = (_accepted(inbox, tmp_path, "user", index) for index in range(2))
        assert store.reinforce_candidate(left).applied
        assert store.reinforce_candidate(right).applied
        assert len(store.graph.events()) == 2
        history = _disk_history(store)
        assert store.reinforce_candidate(left).reason == "duplicate_evidence"
        assert _disk_history(store) == history
        assert store.reinforce_candidate(left, related_candidates=(right,)).reason == "backfilled_edges"
    assert len(store.graph.events()) == 3 and all(event["type"] != PRUNED for event in store.graph.events())
    before, history = store.graph.rows(), _disk_history(store)
    assert store.rebuild_from_events()
    assert store.graph.rows() == before and _disk_history(store) == history


@pytest.mark.parametrize("scope", ("user", "project", "session"))
def test_degree_delta_is_small_and_rolls_back_with_failed_projection_write(tmp_path: Path, scope: str) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _ = _seed_degree(tmp_path, scope, tied=True)
        payload = _payload(store, "projection_pruned")
        assert payload["node_ids"] == []
        source, target, relation = _edge_key_from_payload(payload)
        assert len(json.dumps(payload).encode()) < 256
        inbox = GhostInboxStore(tmp_path)
        hub = next(candidate for candidate in inbox.list_candidates() if candidate.conflict_key.endswith("independent_slot_0"))
        peer = next(candidate for candidate in inbox.list_candidates()
                    if candidate.id != hub.id and _candidate_edge_key(hub, candidate) == (source, target, relation))
        before, history = store.graph.rows(), _disk_history(store)

        def fail_after_write(nodes: Iterable[GhostNode], edges: Iterable[GhostEdge]) -> None:
            assert tuple(nodes)
            assert tuple(edges)
            store.graph.replace_rows((), ())
            raise sqlite3.OperationalError("injected after pruning projection mutation")

        with patch.object(store, "_write_state", fail_after_write):
            failed = store.reinforce_candidate(hub, related_candidates=(peer,))
            assert not failed.applied and failed.reason == "store_error"
        assert store.graph.rows() == before and _disk_history(store) == history
        # Capacity pruning does not retire this accepted source permanently.
        assert store.reinforce_candidate(hub, related_candidates=(peer,)).applied
    assert sum(event["type"] == PRUNED for event in store.graph.events()) == 2
    before, history = store.graph.rows(), _disk_history(store)
    reopened = GhostHebbianStore(tmp_path)
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read wall clock")):
        assert reopened.rebuild_from_events() and reopened.rebuild_from_events()
    assert reopened.graph.rows() == before and _disk_history(reopened) == history


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("valid", "null"))
@pytest.mark.parametrize("confirmed", (False, True))
def test_normal_cli_rebuild_consumes_or_rejects_pruning_with_confirmation(
    tmp_path: Path, scope: str, variant: str, confirmed: bool,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _ = _seed_degree(tmp_path, scope, tied=True)
    if variant != "valid":
        _change(store, "projection_pruned", _invalid_payload(store, variant))
    before, history = store.graph.rows(), _disk_history(store)
    args = [sys.executable, "-m", "codey", "ghost", "rebuild-state", "--state-home", str(tmp_path)]
    if confirmed:
        args.append("--yes")
    completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == (2 if not confirmed else 0 if variant == "valid" else 1), completed.stderr
    result: object = json.loads(completed.stdout)
    assert object_fields(result)
    if confirmed:
        assert result["ok"] is (variant == "valid")
    else:
        assert result["error"] == "rebuild-state requires --yes"
    assert store.graph.rows() == before and _disk_history(store) == history


def _legacy_window_without_pruning(store: GhostHebbianStore) -> None:
    # Model the proven old producer format in a fresh fixture window: all
    # original normal events, with only the newly introduced marker absent.
    # Existing original audit rows are retained, never physically deleted.
    before, history = store.graph.rows(), _disk_history(store)
    legacy = tuple(event for event in store.graph.events() if event["type"] != PRUNED)
    with store.graph.transaction():
        store.graph.append(legacy, replace=True)
        store.graph.replace_rows(*before)
    assert _disk_history(store)[:len(history)] == history


@pytest.mark.parametrize("deleted_scope", tuple(RETAINED_SCOPE))
def test_legacy_unrecorded_degree_pruning_before_scope_delete_preserves_current_projection(
    tmp_path: Path, deleted_scope: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _ = _seed_degree(tmp_path, RETAINED_SCOPE[deleted_scope], tied=True)
        candidate = _accepted(GhostInboxStore(tmp_path), tmp_path, deleted_scope, 999)
        assert store.reinforce_candidate(candidate).applied
        assert store.delete_scope(deleted_scope, project=str(tmp_path / "project"), session_id="s1") == {
            "nodes": 1, "edges": 0,
        }
    _legacy_window_without_pruning(store)
    _assert_rejected(store, tmp_path)


def test_real_node_delta_and_legacy_window_protection_remain_resettable_with_controls_off(tmp_path: Path) -> None:
    store, excluded = _seed_capacity(tmp_path, "user", "over_cap")
    payload = _payload(store, "projection_pruned")
    assert payload == {"node_ids": sorted(excluded), "edge_keys": []}
    assert len(json.dumps(payload).encode()) < 128
    assert sum(event["type"] == "ghost_hebbian_node_upsert" for event in store.graph.events()) == 501
    assert sum(event["type"] == PRUNED for event in store.graph.events()) == 1
    before, history = store.graph.rows(), _disk_history(store)
    assert store.rebuild_from_events() and store.graph.rows() == before and _disk_history(store) == history
    _legacy_window_without_pruning(store)
    _assert_rejected(store, tmp_path)
    history = _disk_history(store)
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
        assert surface.inbox.set_control(control, False, scope="user")
    status, result = surface.dispatch_action({"action": "reset_all", "scope": "user", "confirm": True})
    assert status == 200 and result["ok"] is True, result
    reopened = GhostHebbianStore(tmp_path)
    assert reopened.rebuild_from_events()
    assert not reopened.list_nodes() and not reopened.list_edges()
    assert _disk_history(reopened)[:len(history)] == history


@pytest.mark.parametrize("deleted_scope", tuple(RETAINED_SCOPE))
def test_same_clock_relearning_and_repeated_pruning_keep_every_transition_replayable(
    tmp_path: Path, deleted_scope: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, _ = _seed_degree(tmp_path, RETAINED_SCOPE[deleted_scope], tied=True)
        inbox = GhostInboxStore(tmp_path)
        hub = next(candidate for candidate in inbox.list_candidates() if candidate.conflict_key.endswith("independent_slot_0"))
        peers = tuple(candidate for candidate in inbox.list_candidates() if candidate.id != hub.id)
        assert len(peers) == 9
        for _ in range(9):
            live = {(edge.source, edge.target, edge.relation) for edge in store.list_edges()}
            [missing] = [peer for peer in peers if _candidate_edge_key(hub, peer) not in live]
            result = store.reinforce_candidate(hub, related_candidates=(missing,), reward=1.0)
            assert result.applied and len(result.edges) == 1, result
            assert len(store.list_edges()) == 8
        extra = _accepted(inbox, tmp_path, deleted_scope, 999)
        assert store.reinforce_candidate(extra).applied
        assert store.delete_scope(deleted_scope, project=str(tmp_path / "project"), session_id="s1") == {
            "nodes": 1, "edges": 0,
        }
    before, history = store.graph.rows(), _disk_history(store)
    accepted: list[bool] = []
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read wall clock")):
        for _ in range(2):
            accepted.append(store.rebuild_from_events())
            assert store.graph.rows() == before and _disk_history(store) == history
    measurement: dict[str, object] = {
        "deleted_scope": deleted_scope, "accepted": accepted, "node_count": len(before[0]), "edge_count": len(before[1]),
        "pruning_events": sum(event["type"] == PRUNED for event in store.graph.events()),
    }
    print("REPEATED_PRUNING_CELL " + json.dumps(measurement, sort_keys=True))
    assert accepted == [True, True], "same-clock repeated transitions were lost from the committed log"
