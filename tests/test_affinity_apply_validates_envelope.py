"""Direct affinity apply validates the full envelope before mutating state.

Locks: apply_affinity_event rejects events missing event_id/ts/schema,
wrong schema, bad snapshot rows, incomplete scope-deletion/decay payloads,
and leaves the input state untouched on failure. Single replay path stays
strict: illegal rows fail instead of being skipped.
"""
from __future__ import annotations

import copy

import pytest

from codey.ghost.affinity_events import apply_affinity_event


def _node_event():
    from codey.ghost.affinity_events import _node_reinforced_event
    from codey.ghost.affinity_model import AffinityNodeSpec

    spec = AffinityNodeSpec(
        kind="research_concept", key="k", label="concept:k",
        scope="user", scope_ref="", confidence=0.8, reward=0.5,
        source_refs=("research_interest:k:1",), evidence_refs=(),
        metadata={"source": "test"},
    )
    return _node_reinforced_event(spec, ts="2999-01-01T00:00:00Z")


def _state_with_one_node():
    state: dict = {"nodes": {}, "edges": {}}
    apply_affinity_event(state, _node_event())
    assert len(state["nodes"]) == 1
    return state


def test_scope_deleted_missing_envelope_rejected_without_mutation():
    state = _state_with_one_node()
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, {"type": "ghost_affinity_scope_deleted", "payload": {"scope": "user", "scope_ref": ""}})
    assert state["nodes"].keys() == before["nodes"].keys()


def test_snapshot_with_bad_row_types_rejected_without_mutation():
    state = _state_with_one_node()
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, {"type": "ghost_affinity_snapshot", "nodes": "bad", "edges": "bad"})
    assert state["nodes"].keys() == before["nodes"].keys()


def test_decay_with_empty_payload_rejected_without_mutation():
    state = _state_with_one_node()
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, {"type": "ghost_affinity_decay_applied", "payload": {}})
    assert state["nodes"].keys() == before["nodes"].keys()


def test_scope_deleted_with_extra_field_rejected_without_mutation():
    from codey.ghost.affinity_events import _scope_deleted_event

    state = _state_with_one_node()
    before = copy.deepcopy(state)
    event = _scope_deleted_event("user", "", removed_nodes=1, removed_edges=0, ts="2999-01-01T00:00:00Z")
    event["payload"] = dict(event["payload"])
    event["payload"]["extra"] = 1
    with pytest.raises(ValueError):
        apply_affinity_event(state, event)
    assert state["nodes"].keys() == before["nodes"].keys()


def test_wrong_schema_version_rejected_without_mutation():
    state = _state_with_one_node()
    before = copy.deepcopy(state)
    event = _node_event()
    event = dict(event)
    event["schema_version"] = 9999
    with pytest.raises(ValueError):
        apply_affinity_event(state, event)
    assert state["nodes"].keys() == before["nodes"].keys()


def test_replay_fails_on_illegal_row_instead_of_skipping():
    from codey.ghost.affinity_events import replay_affinity_events

    with pytest.raises(ValueError):
        replay_affinity_events([_node_event(), {"type": "ghost_affinity_snapshot", "nodes": "bad", "edges": "bad"}])
