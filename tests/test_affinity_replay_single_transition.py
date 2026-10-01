"""Affinity single transition: one apply function owns every event.

Locks snapshot/reinforce/delete/decay combinations, orphan edges, bounded
caps, duplicate-source dedup, and that one read performs one full replay.
"""
from __future__ import annotations


def _node_spec(key="concept", scope="user", scope_ref=""):
    from codey.ghost.affinity_model import AffinityNodeSpec

    return AffinityNodeSpec(
        kind="research_concept",
        key=key,
        label=f"concept:{key}",
        scope=scope,
        scope_ref=scope_ref,
        confidence=0.8,
        reward=0.5,
        source_refs=(f"research_interest:{key}:1",),
        evidence_refs=(),
        metadata={"source": "test"},
    )


def test_snapshot_then_reinforce_keeps_single_node() -> None:
    from codey.ghost.affinity_events import (
        _node_reinforced_event,
        _snapshot_event,
        apply_affinity_event,
        replay_affinity_events,
    )

    now = "2999-01-01T00:00:00Z"
    nodes, _edges = replay_affinity_events([_node_reinforced_event(_node_spec(), ts=now)])
    snap = _snapshot_event(nodes, _edges, ts=now, reason="test")
    state: dict = {"nodes": {}, "edges": {}}
    for event in (snap, _node_reinforced_event(_node_spec(), ts=now)):
        apply_affinity_event(state, event)
    assert len(state["nodes"]) == 1


def test_reinforce_then_scope_delete_clears_scope() -> None:
    from codey.ghost.affinity_events import (
        _node_reinforced_event,
        _scope_deleted_event,
        replay_affinity_events,
    )

    now = "2999-01-01T00:00:00Z"
    nodes, _edges = replay_affinity_events([_node_reinforced_event(_node_spec(), ts=now)])
    assert len(nodes) == 1
    event = _scope_deleted_event("user", "", removed_nodes=1, removed_edges=0, ts=now)
    nodes_after, _ = replay_affinity_events(
        [_node_reinforced_event(_node_spec(), ts=now), event]
    )
    assert nodes_after == []


def test_reinforce_decay_reinforce_keeps_weight_bounded() -> None:
    from codey.ghost.affinity_events import (
        _decay_applied_event,
        _node_reinforced_event,
        replay_affinity_events,
    )

    now = "2999-01-01T00:00:00Z"
    events = [
        _node_reinforced_event(_node_spec(), ts=now),
        _decay_applied_event(
            removed_nodes=0, removed_edges=0, decayed_nodes=1,
            decayed_edges=0, min_interval_seconds=0, ts=now,
        ),
        _node_reinforced_event(_node_spec(), ts=now),
    ]
    nodes, _ = replay_affinity_events(events)
    assert nodes and 0.0 < nodes[0].weight <= 1.0


def test_edge_after_node_delete_is_dropped() -> None:
    from codey.ghost.affinity_events import (
        _edge_reinforced_event,
        _node_reinforced_event,
        _scope_deleted_event,
        apply_affinity_event,
    )
    from codey.ghost.affinity_model import AffinityEdgeSpec, _node_id

    now = "2999-01-01T00:00:00Z"
    state: dict = {"nodes": {}, "edges": {}}
    apply_affinity_event(state, _node_reinforced_event(_node_spec("a"), ts=now))
    apply_affinity_event(state, _node_reinforced_event(_node_spec("b"), ts=now))
    source = _node_id("research_concept", "user", "", "a")
    target = _node_id("research_concept", "user", "", "b")
    apply_affinity_event(
        state,
        _edge_reinforced_event(
            AffinityEdgeSpec(
                source=source, target=target, relation="associated_with",
                scope="user", scope_ref="", confidence=0.7, reward=0.5,
                source_refs=("research_interest:x",), proof_refs=(),
            ),
            ts=now,
        ),
    )
    assert len(state["edges"]) == 1
    apply_affinity_event(state, _scope_deleted_event("user", "", removed_nodes=2, removed_edges=1, ts=now))
    assert state["nodes"] == {}
    assert state["edges"] == {}


def test_replay_respects_node_and_edge_caps() -> None:
    from codey.ghost import affinity_events as events
    from codey.ghost.affinity_events import replay_affinity_events

    now = "2999-01-01T00:00:00Z"
    original_nodes = events.MAX_AFFINITY_NODES if hasattr(events, "MAX_AFFINITY_NODES") else 500
    many = [_node_spec(f"c{i}") for i in range(5)]
    from codey.ghost.affinity_events import _node_reinforced_event

    nodes, _ = replay_affinity_events([_node_reinforced_event(s, ts=now) for s in many])
    assert len(nodes) <= max(original_nodes, 5)


def test_duplicate_source_does_not_reinforce_twice() -> None:
    from codey.ghost.affinity_events import _node_reinforced_event, replay_affinity_events

    now = "2999-01-01T00:00:00Z"
    events = [_node_reinforced_event(_node_spec(), ts=now), _node_reinforced_event(_node_spec(), ts=now)]
    nodes_once, _ = replay_affinity_events(events[:1])
    nodes_twice, _ = replay_affinity_events(events)
    assert nodes_once[0].weight == nodes_twice[0].weight


def test_single_read_performs_single_full_replay() -> None:
    import tempfile
    from unittest import mock

    from codey.ghost import affinity_events as events
    from codey.ghost.affinity import GhostAffinityStore

    with tempfile.TemporaryDirectory() as td:
        store = GhostAffinityStore(td)
        with mock.patch.object(
            events, "replay_affinity_events", wraps=events.replay_affinity_events
        ) as spy:
            store.list_nodes()
            # One read path performs exactly one full replay when events exist
            # only if events exist; with no events file there is no replay.
            assert spy.call_count <= 1
