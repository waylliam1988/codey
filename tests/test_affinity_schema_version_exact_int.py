"""Affinity schema_version must be the exact int, never bool/float/str.

Locks P2: ``apply_affinity_event`` accepts only ``type(schema_version) is
int`` equal to ``AFFINITY_SCHEMA_VERSION``. ``True`` (== 1) and ``1.0``
(== 1) previously passed the ``!=`` check and mutated state; they must now
raise without mutating.
"""
from __future__ import annotations

import copy

import pytest


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


@pytest.mark.parametrize("bad_version", [True, False, 1.0, 0.0, "1", None])
def test_non_exact_int_schema_rejected_without_mutation(bad_version):
    from codey.ghost.affinity_events import apply_affinity_event

    state: dict = {"nodes": {}, "edges": {}}
    event = _node_event()
    event = dict(event)
    event["schema_version"] = bad_version
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, event)
    assert state == before


def test_missing_schema_rejected_without_mutation():
    from codey.ghost.affinity_events import apply_affinity_event

    state: dict = {"nodes": {}, "edges": {}}
    event = _node_event()
    event = {k: v for k, v in event.items() if k != "schema_version"}
    before = copy.deepcopy(state)
    with pytest.raises(ValueError):
        apply_affinity_event(state, event)
    assert state == before


def test_exact_int_schema_still_accepted():
    from codey.ghost.affinity_events import apply_affinity_event
    from codey.ghost.affinity_model import AFFINITY_SCHEMA_VERSION

    assert type(AFFINITY_SCHEMA_VERSION) is int
    state: dict = {"nodes": {}, "edges": {}}
    event = _node_event()
    assert type(event["schema_version"]) is int
    apply_affinity_event(state, event)
    assert len(state["nodes"]) == 1
