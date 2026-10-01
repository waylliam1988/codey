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


def test_scope_delete_removes_existing_nodes_and_edges() -> None:
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


def test_edge_referencing_deleted_node_is_rejected() -> None:
    import pytest

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
    apply_affinity_event(state, _scope_deleted_event("user", "", removed_nodes=2, removed_edges=0, ts=now))
    assert state["nodes"] == {}
    source = _node_id("research_concept", "user", "", "a")
    target = _node_id("research_concept", "user", "", "b")
    with pytest.raises(ValueError):
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


def test_replay_enforces_node_edge_and_fanout_caps() -> None:
    from unittest import mock

    from codey.ghost import affinity_events as events_owner
    from codey.ghost.affinity_events import _edge_reinforced_event, _node_reinforced_event, replay_affinity_events
    from codey.ghost.affinity_model import AffinityEdgeSpec, _node_id

    now = "2999-01-01T00:00:00Z"
    node_events = [_node_reinforced_event(_node_spec(f"c{i}"), ts=now) for i in range(6)]
    with mock.patch.object(events_owner, "MAX_AFFINITY_NODES", 3):
        nodes, _ = replay_affinity_events(node_events)
        assert len(nodes) <= 3
    # Edges: endpoints must exist and fanout is bounded.
    center = _node_spec("center")
    leaves = [_node_spec(f"leaf{i}") for i in range(5)]
    base = [_node_reinforced_event(center, ts=now)] + [_node_reinforced_event(s, ts=now) for s in leaves]
    nodes, _ = replay_affinity_events(base)
    node_ids = {n.id for n in nodes}
    center_id = _node_id("research_concept", "user", "", "center")
    assert center_id in node_ids
    edge_events = []
    for i in range(5):
        target = _node_id("research_concept", "user", "", f"leaf{i}")
        edge_events.append(_edge_reinforced_event(
            AffinityEdgeSpec(
                source=center_id, target=target, relation="associated_with",
                scope="user", scope_ref="", confidence=0.7, reward=0.5,
                source_refs=(f"research_interest:e{i}",), proof_refs=(),
            ),
            ts=now,
        ))
    _, edges = replay_affinity_events(base + edge_events)
    assert edges, "edges under caps must survive"
    assert all(e.source in node_ids and e.target in node_ids for e in edges)
    with mock.patch.object(events_owner, "MAX_EDGE_OUT_DEGREE", 2):
        _, fanout_edges = replay_affinity_events(base + edge_events)
        from collections import Counter

        out_counts = Counter(e.source for e in fanout_edges)
        assert all(count <= 2 for count in out_counts.values())


def test_duplicate_source_does_not_reinforce_twice() -> None:
    from codey.ghost.affinity_events import _node_reinforced_event, replay_affinity_events

    now = "2999-01-01T00:00:00Z"
    events = [_node_reinforced_event(_node_spec(), ts=now), _node_reinforced_event(_node_spec(), ts=now)]
    nodes_once, _ = replay_affinity_events(events[:1])
    nodes_twice, _ = replay_affinity_events(events)
    assert nodes_once[0].weight == nodes_twice[0].weight


def test_single_read_with_events_performs_single_full_replay() -> None:
    import tempfile
    from unittest import mock

    from codey.ghost import affinity as store_module
    from codey.ghost.affinity import GhostAffinityStore
    from codey.ghost.affinity_events import _node_reinforced_event

    now = "2999-01-01T00:00:00Z"
    with tempfile.TemporaryDirectory() as td:
        store = GhostAffinityStore(td)
        store._event_log().write_atomic([_node_reinforced_event(_node_spec("solo"), ts=now)])
        with mock.patch.object(
            store_module, "replay_affinity_events", wraps=store_module.replay_affinity_events
        ) as spy:
            nodes = store.list_nodes()
            assert [n.key for n in nodes] == ["solo"]
            assert spy.call_count == 1
