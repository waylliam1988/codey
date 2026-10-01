"""Affinity events own transitions, validation, and replay.

Locks that ``affinity_events`` replays real node/edge rows, rejects orphan
edges, keeps snapshot and incremental replay consistent, is called by the
Store through the real functions, and never imports the Store or sources.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports_of(path: Path) -> set[str]:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_events_module_replays_real_rows() -> None:
    from codey.ghost.affinity_events import replay_affinity_events
    from codey.ghost.affinity_model import AffinityNodeSpec

    now = "2999-01-01T00:00:00Z"
    spec = AffinityNodeSpec(
        kind="research_concept",
        key="concept",
        label="concept:concept",
        scope="user",
        scope_ref="",
        confidence=0.8,
        reward=0.5,
        source_refs=("research_interest:1",),
        evidence_refs=(),
        metadata={"source": "test"},
    )
    from codey.ghost.affinity_events import _node_reinforced_event

    events = [_node_reinforced_event(spec, ts=now)]
    nodes, edges = replay_affinity_events(events)
    assert len(nodes) == 1
    assert nodes[0].key == "concept"
    assert edges == []


def test_events_reject_orphan_edge() -> None:
    import pytest

    from codey.ghost.affinity_events import _edge_reinforced_event, replay_affinity_events
    from codey.ghost.affinity_model import AffinityEdgeSpec

    spec = AffinityEdgeSpec(
        source="gan_missing_a",
        target="gan_missing_b",
        relation="associated_with",
        scope="user",
        scope_ref="",
        confidence=0.7,
        reward=0.5,
        source_refs=("research_interest:1",),
        proof_refs=(),
    )
    with pytest.raises(ValueError):
        replay_affinity_events([_edge_reinforced_event(spec, ts="2999-01-01T00:00:00Z")])


def test_snapshot_and_incremental_replay_agree() -> None:
    from codey.ghost.affinity_events import (
        _node_reinforced_event,
        _snapshot_event,
        replay_affinity_events,
    )
    from codey.ghost.affinity_model import AffinityNodeSpec

    now = "2999-01-01T00:00:00Z"
    spec = AffinityNodeSpec(
        kind="task_type",
        key="coding",
        label="task_type:coding",
        scope="user",
        scope_ref="",
        confidence=0.7,
        reward=0.5,
        source_refs=("work_item:x:queued:now",),
        evidence_refs=(),
        metadata={"source": "work_queue"},
    )
    incremental = [_node_reinforced_event(spec, ts=now)]
    nodes_inc, edges_inc = replay_affinity_events(incremental)
    snapshot = [_snapshot_event(nodes_inc, edges_inc, ts=now, reason="test")]
    nodes_snap, edges_snap = replay_affinity_events([*incremental, *snapshot])
    # Snapshot truncates history but preserves full row payloads.
    assert [n.to_payload() for n in nodes_snap] == [n.to_payload() for n in nodes_inc]
    assert [e.to_payload() for e in edges_snap] == [e.to_payload() for e in edges_inc]


def test_store_calls_events_owner_with_real_function() -> None:
    import inspect

    from codey.ghost import affinity as store_module
    from codey.ghost import affinity_events as events

    assert store_module.replay_affinity_events is events.replay_affinity_events
    src = inspect.getsource(store_module.GhostAffinityStore._load_state_for_read_unlocked)
    assert "replay_affinity_events" in src


def test_events_leaf_imports_neither_store_nor_sources() -> None:
    imports = _imports_of(ROOT / "codey" / "ghost" / "affinity_events.py")
    assert "codey.ghost.affinity" not in imports
    assert "codey.ghost.affinity_sources" not in imports
    text = (ROOT / "codey" / "ghost" / "affinity_events.py").read_text(encoding="utf-8-sig")
    assert "with_file_lock" not in text
    assert "write_json_atomic" not in text
