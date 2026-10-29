"""Corrupt continuation data must stop before publishing recovered facts."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.research_continuation import ResearchContinuation, restore_note_proposals
from codey.providers.response_record import json_value
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.pipeline import ResearchContinuationError
from codey.research.plan_executor import PlanExecutionResult
from codey.research.query_planner import QueryCandidate, ResearchPlan
from codey.research.tools import ResearchTools
from codey.runtime.core.operation_state import RuntimeOperationState, new_operation_state


def _material():
    ledger = ResearchLedger()
    ledger.record_search("durable recovery", [{"title": "Original", "url": "https://example.com/source"}])
    ledger.record_open("https://example.com/source", "https://example.com/source", "Original", "original source text")
    material = PlanExecutionResult(queries_executed=("durable recovery",),
        opened_sources=(ledger.opened_sources[0].to_dict(),), previews=("original source text",),
        fresh_source_urls=("https://example.com/source",), fresh_source_count=1, stop_reason="max_sources")
    observations = [ledger_observation(ledger, "web_search"),
                    ledger_observation(ledger, "open_url", url="https://example.com/source")]
    plan = ResearchPlan("research_plan:0123456789abcdef",
                        query_candidates=(QueryCandidate("research_query:0123456789abcdef", "durable recovery"),))
    return material, observations, plan


@pytest.mark.parametrize("field,value", [
    ("fresh_source_count", True), ("fresh_source_count", 0), ("skipped_count", -1),
    ("stop_reason", []), ("stop_reason", "invented"), ("queries_executed", [False]),
    ("previews", [None]), ("errors", [{}]), ("opened_sources", ["not a source"]),
    ("fresh_source_urls", ["https://example.com/source", "https://example.com/source"]),
])
def test_invalid_material_is_rejected(field, value):
    payload = json_value(_material()[0])
    payload[field] = value
    with pytest.raises(ResearchContinuationError):
        ResearchContinuation.decode_material(payload)


@pytest.mark.parametrize("corruption", ["body", "query", "source", "shape", "missing"])
def test_corrupt_observation_does_not_publish_partial_ledger(corruption):
    material, observations, plan = _material()
    observations = copy.deepcopy(observations)
    if corruption == "body":
        observations[1]["text"] = "changed source text"
    elif corruption == "query":
        observations[0]["search"]["query"] = "another query"
    elif corruption == "source":
        material = replace(material, opened_sources=({**material.opened_sources[0], "title": "Different"},))
    elif corruption == "shape":
        observations[1] = {"evidence": []}
    else:
        observations.pop()
    tools = SimpleNamespace(ledger=ResearchLedger())
    with pytest.raises(ResearchContinuationError):
        ResearchContinuation.restore_material(tools, plan, material, observations)
    assert tools.ledger.searches == []
    assert tools.ledger.opened_sources == []


def test_valid_material_rebuilds_original_source():
    material, observations, plan = _material()
    tools = SimpleNamespace(ledger=ResearchLedger())
    ResearchContinuation.restore_material(tools, plan, material, observations)
    assert tools.ledger.source_text_for_url(material.fresh_source_urls[0]) == "original source text"
    assert tools.ledger.opened_sources[0].to_dict() == material.opened_sources[0]


def test_restored_note_commit_is_idempotent_and_conflict_preserves_vault(tmp_path, monkeypatch):
    store = KnowledgeStore(tmp_path)
    try:
        note = KnowledgeNote(id="original-proposal", title="Original", body="Original body", type="fact",
                             created="2026-01-01T00:00:00+00:00", updated="2026-01-01T00:00:00+00:00")
        canonical = {"knowledge_notes": {"created": [note.id], "updated": [], "proposals": [json_value(note)]}}
        store.write_note(replace(note))
        original = store.read_note(note.id).to_markdown()
        tools = ResearchTools(search=object(), store=store, changes=KnowledgeChanges(root=store.root)).create_staged()
        restore_note_proposals(tools, canonical)
        monkeypatch.setattr(store, "write_note", lambda *args, **kwargs: pytest.fail("original proposal was rewritten"))
        tools.store.commit_to(store)
        assert store.read_note(note.id).to_markdown() == original
        assert tools.created_ids == [note.id]
        canonical["knowledge_notes"]["proposals"][0]["body"] = "Conflicting body"
        with pytest.raises(ResearchContinuationError, match="differs from its original proposal"):
            restore_note_proposals(tools, canonical)
        assert store.read_note(note.id).to_markdown() == original
    finally:
        store.index.close()


@pytest.mark.parametrize("value", ["trace:guess", "artifact_" + "g" * 24, "artifact_" + "a" * 25, True])
def test_operation_rejects_invalid_continuation_reference(value):
    state = new_operation_state(session_id="s", run_id="r", project="", provider_id="local",
                                turn_budget=4, max_repair_rounds=0, task_kind="research")
    payload = state.to_payload()
    payload["continuation_ref"] = value
    assert RuntimeOperationState.from_payload(payload) is None


def test_continuation_reference_is_research_data_not_a_second_position():
    state = new_operation_state(session_id="s", run_id="r", project="", provider_id="local",
                                turn_budget=4, max_repair_rounds=0, task_kind="research")
    state = replace(state, continuation_ref="artifact_" + "a" * 24)
    assert RuntimeOperationState.from_payload(state.to_payload()) == state
    assert RuntimeOperationState.from_payload(replace(state, task_kind="project").to_payload()) is None


@pytest.mark.parametrize("schema", [True, 1.0, "1"])
def test_continuation_artifact_version_requires_an_integer(schema):
    reference, digest = "artifact_" + "a" * 24, "a" * 64
    artifact = SimpleNamespace(owner_task_id="r", owner_effect_id=None, sha256=digest,
        metadata={"kind": "research_continuation", "ref": {
            "handle": reference, "sha256": digest, "session_id": "s", "run_id": "r"}})
    durable = SimpleNamespace(artifact=lambda _: artifact)
    outputs = SimpleNamespace(read_json_artifact=lambda _: {"schema_version": schema, "kind": "research_continuation"})
    continuation = ResearchContinuation(SimpleNamespace(durable=durable), outputs, session_id="s", run_id="r")
    with pytest.raises(ResearchContinuationError, match="invalid continuation artifact"):
        continuation.read(reference, "research_continuation")


def test_schema_five_migration_is_idempotent_and_preserves_position():
    from codey.durable.checkpoint import operation_from_checkpoint

    state = new_operation_state(session_id="s", run_id="r", project="", provider_id="local",
                                turn_budget=4, max_repair_rounds=0, task_kind="research")
    payload = {**state.to_payload(), "schema_version": 5}
    task = SimpleNamespace(conversation_id="s", task_id="r", state={"operation": payload})
    restored = operation_from_checkpoint(task, "s")
    assert restored == state
    task.state["operation"] = restored.to_payload()
    assert operation_from_checkpoint(task, "s") == restored
