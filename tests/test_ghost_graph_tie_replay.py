"""Real cap transitions and semantic tie ordering for both shared bounds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.graph_primitives import bound_graph_edges, bound_graph_nodes
from codey.ghost.hebbian import MAX_EDGE_OUT_DEGREE, GhostEdge, GhostHebbianStore, GhostNode, node_id_for_candidate
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from tests.test_ghost_hebbian import _accept, _ingest_one, _signal

NOW = "2026-11-18T00:00:00Z"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _edge_key(edge: GhostEdge) -> tuple[str, str, str]:
    return edge.source, edge.target, edge.relation


def _candidate_edge_key(hub: GhostMemoryCandidate, peer: GhostMemoryCandidate) -> tuple[str, str, str]:
    left, right = sorted((node_id_for_candidate(hub), node_id_for_candidate(peer)))
    return left, right, "coactivated_with"


def _accepted(inbox: GhostInboxStore, home: Path, scope: str, index: int) -> GhostMemoryCandidate:
    return _accept(inbox, _ingest_one(
        inbox, _signal(scope=scope, confidence=1.0, conflict_key=f"independent_slot_{index}",
                       value_key=f"choice_{index}", summary=f"Prefer independent choice {index}.",
                       quote=f"Use independent choice {index}."),
        project=str(home / "project"), session_id="s1", run_id="tie-run",
    ))


def _seed(home: Path, scope: str, *, tied: bool) -> tuple[GhostHebbianStore, tuple[str, str, str]]:
    inbox = GhostInboxStore(home)
    store = GhostHebbianStore(home)
    hub = _accepted(inbox, home, scope, 0)
    peers = tuple(_accepted(inbox, home, scope, index + 1) for index in range(MAX_EDGE_OUT_DEGREE + 1))
    for candidate in (hub, *peers):
        result = store.reinforce_candidate(candidate)
        assert result.applied and result.node is not None, ("INCOMPLETE_FIXTURE", result)
        assert result.node.scope == scope and result.node.scope_ref == candidate.scope_ref
    def candidate_order(peer: GhostMemoryCandidate) -> tuple[str, str, str]:
        return _candidate_edge_key(hub, peer)

    ordered = sorted(peers, key=candidate_order, reverse=True)
    # Insert a reverse-identity prefix, fill the real degree cap, then give a
    # previously unseen edge enough weight to displace one old edge.
    for index, peer in enumerate(ordered):
        if index == MAX_EDGE_OUT_DEGREE:
            assert len(store.list_edges()) == MAX_EDGE_OUT_DEGREE
            reward = 1.0
        else:
            reward = 0.5 if tied else 0.1 + index * 0.05
        result = store.reinforce_candidate(hub, reward=reward, related_candidates=(peer,))
        assert result.applied and len(result.edges) == 1, ("INCOMPLETE_FIXTURE", result)
    strong = _candidate_edge_key(hub, ordered[-1])
    edges = store.list_edges()
    assert len(edges) == MAX_EDGE_OUT_DEGREE and _edge_key(edges[0]) == strong
    assert len(store.list_nodes()) == MAX_EDGE_OUT_DEGREE + 2
    events = store.graph.events()
    assert sum(event["type"] == "ghost_hebbian_edge_upsert" for event in events) == MAX_EDGE_OUT_DEGREE + 1
    return store, strong


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("restart", ("warm", "cold"))
@pytest.mark.parametrize("weights", ("tie", "unique"))
def test_normal_degree_transition_and_fixed_log_rebuild_have_identical_state(
    tmp_path: Path, scope: str, restart: str, weights: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store, strong = _seed(tmp_path, scope, tied=weights == "tie")
        before = store.graph.rows()
        events = store.graph.events()
        if restart == "cold":
            store = GhostHebbianStore(tmp_path)
        digests = [_digest(before)]
        for _ in range(2):
            assert store.rebuild_from_events(), "INCOMPLETE_FIXTURE: valid replay was rejected"
            assert store.graph.events() == events, "rebuild rewrote original history"
            assert len(store.list_edges()) == MAX_EDGE_OUT_DEGREE
            assert _edge_key(store.list_edges()[0]) == strong
            digests.append(_digest(store.graph.rows()))
        same = digests == [digests[0]] * 3
        measurement: dict[str, object] = {
            "kind": "public_degree", "scope": scope, "restart": restart, "weights": weights,
            "valid": True, "digests": digests, "same_state": same,
        }
        print("GRAPH_TIE_CELL " + json.dumps(measurement, sort_keys=True))
        assert same, "valid fixed-log replay changed ordinary writer state at the real degree cap"


def _node(identity: str) -> GhostNode:
    return GhostNode(
        id=identity, kind="style_preference", label="Prefer " + identity,
        conflict_key="slot_" + identity, value_key=identity, status="active", scope="user", scope_ref="",
        weight=0.5, confidence=1.0, candidate_ids=("candidate:" + identity,),
        evidence_refs=("source:" + identity,), created_at=NOW, updated_at=NOW, last_reinforced_at=NOW,
    )


def _edge(target: str) -> GhostEdge:
    return GhostEdge(
        source="a", target=target, relation="coactivated_with", weight=0.5,
        candidate_ids=("candidate:a", "candidate:" + target), evidence_refs=("source:" + target,),
        created_at=NOW, updated_at=NOW, last_reinforced_at=NOW,
    )


def _payloads(rows: Iterable[GhostNode | GhostEdge]) -> list[dict[str, object]]:
    return [row.to_payload() for row in rows]


@pytest.mark.parametrize("order", range(6))
def test_node_cap_ties_use_semantic_identity_in_every_permutation(order: int) -> None:
    nodes = (_node("a"), _node("b"), _node("c"))
    rows = list(permutations(nodes))[order]
    bounded = bound_graph_nodes(rows, min_active_weight=0.05, limit=2)
    same = [node.id for node in bounded] == ["a", "b"]
    measurement: dict[str, object] = {
        "kind": "node_tie", "order": order, "valid": True,
        "digest": _digest(_payloads(bounded)), "same_state": same,
    }
    print("GRAPH_TIE_CELL " + json.dumps(measurement, sort_keys=True))
    assert same, "node cap tie depends on input order instead of semantic identity"


@pytest.mark.parametrize("order", range(6))
def test_edge_degree_ties_use_semantic_identity_in_every_permutation(order: int) -> None:
    edges = (_edge("b"), _edge("c"), _edge("d"))
    rows = list(permutations(edges))[order]
    bounded = bound_graph_edges(rows, node_ids={"a", "b", "c", "d"}, min_weight=0.01,
                                limit=10, max_out_degree=1)
    same = [_edge_key(edge) for edge in bounded] == [("a", "b", "coactivated_with")]
    measurement: dict[str, object] = {
        "kind": "edge_tie", "order": order, "valid": True,
        "digest": _digest(_payloads(bounded)), "same_state": same,
    }
    print("GRAPH_TIE_CELL " + json.dumps(measurement, sort_keys=True))
    assert same, "edge degree tie depends on input order instead of semantic identity"
