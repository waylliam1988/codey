"""Corrupt continuation data must stop before publishing recovered facts."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.research_continuation import ResearchContinuation, ResearchInvocation, restore_note_proposals
from codey.providers.response_record import json_value
from codey.research.pipeline import ResearchContinuationError
from codey.research.tools import ResearchTools
from codey.runtime.core.operation_payload import RuntimeOperationState
from codey.runtime.core.operation_state import new_operation_state


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


@pytest.mark.parametrize("changed", ["purpose", "prompt", "plan_ref"])
def test_invocation_descriptor_rejects_recomputed_domain_intent(changed):
    from codey.operations.kernel_contracts import KernelRunRequest, KernelTransportDeps
    from codey.policies.task_policy import TaskPolicy

    continuation = object.__new__(ResearchContinuation)
    state = SimpleNamespace(writer_attempt=1, work_ref="original-settled-result")
    index = {"invocations": {}}
    session = SimpleNamespace(task_text="Original question", handoff="Original evidence",
                              policy=TaskPolicy(frozenset({"control"})), max_turns=4)
    request = KernelRunRequest(transport=KernelTransportDeps(provider=object(), effect_scope="original-scope"))
    invocation = ResearchInvocation(2, "investigate", "original-plan")
    continuation.bind_invocation(state, index, invocation, session, request)
    assert index["invocations"]["2"]["input_refs"] == ["original-settled-result", "original-plan"]
    continuation.bind_invocation(state, index, invocation, session, request)
    if changed == "prompt":
        session.task_text = "Recomputed question"
    else:
        invocation = replace(invocation, **{changed: "synthesize" if changed == "purpose" else "changed-plan"})
    with pytest.raises(ResearchContinuationError, match="frozen purpose or input"):
        continuation.bind_invocation(state, index, invocation, session, request)


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
