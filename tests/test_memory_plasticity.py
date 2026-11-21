"""Offline mechanisms preserve scope, capacity and recovery contracts."""

from dataclasses import replace

import pytest

from codey.memory.contracts import DeleteScopeRequest, MemoryQuery
from codey.types.json_value import JsonObject
from tests.test_memory_boundary import NOW, SCOPE, event
from tools.memory_plasticity_mechanisms import MECHANISMS, Mechanism, PlasticityLimits, ResearchMemory


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_research_state_is_bounded_replayable_and_scope_isolated(mechanism: Mechanism) -> None:
    model = ResearchMemory(mechanism, PlasticityLimits(nodes=8, edges=12))
    events = [replace(event(str(index)), payload={"symbols": ["cue", str(index)]}) for index in range(30)]
    for value in events:
        model.observe(value)
    assert len(model.counts) <= 8 and len(model.weights) <= 12
    before = model.snapshot()
    restored = ResearchMemory(mechanism, model.limits)
    restored.restore(before)
    assert restored.snapshot() == before
    assert model.retrieve(MemoryQuery("cue", ("project:another",), NOW)).items == ()
    model.delete_scope(DeleteScopeRequest(SCOPE))
    assert model.retrieve(MemoryQuery("cue", (SCOPE,), NOW)).items == ()


def test_temporal_context_changes_a_prediction_without_changing_query_or_training() -> None:
    models = [ResearchMemory("frequency"), ResearchMemory("temporal")]
    sequence = ["popular"] * 20 + [value for _ in range(5) for value in ("cue", "target")]
    for index, value in enumerate(sequence):
        for model in models:
            model.observe(replace(event(str(index)), payload={"symbols": [value]}))
    query = MemoryQuery("cue", (SCOPE,), NOW, max_items=1)
    assert models[0].retrieve(query).items[0].text == "popular"
    assert models[1].retrieve(query).items[0].text == "target"


def test_corrupt_research_snapshot_does_not_partially_replace_state() -> None:
    model = ResearchMemory("temporal")
    model.observe(replace(event("original"), payload={"symbols": ["cue", "target"]}))
    before = model.snapshot()
    corrupt: JsonObject = {**before, "counts": [[SCOPE, "", "other", 10]], "operations": "invalid"}
    with pytest.raises(ValueError):
        model.restore(corrupt)
    assert model.snapshot() == before
