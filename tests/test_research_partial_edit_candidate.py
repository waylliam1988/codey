"""Safety tests for the offline partial-edit candidate, not production behavior."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.runtime.core.models import ToolCall, ToolResult
from tests.support.research_partial_edit_candidate import evidence_last, normalize_edits, treatment


def setup(tmp_path):
    store = KnowledgeStore(tmp_path)
    value = KnowledgeObject("exact-id", "claim", "Original", "model_extraction", aliases=("kept",),
                            conditions={"mode": {"state": "reported", "value": "local OR remote"}},
                            scope="personal", scope_id=store.owner_id, provenance={"original": True})
    store.repository.commit("seed", [KnowledgeMutation(value)])
    result = ToolResult(ToolCall("knowledge_read", {"id": value.id, "revision": 1}),
                        json.dumps(store.repository.read(value.id, 1).to_dict()), ok=True)
    return store, value, SimpleNamespace(_memory_results={"read": result})


def test_partial_edit_keeps_every_unmentioned_field_and_exact_old_version(tmp_path):
    store, value, session = setup(tmp_path)
    try:
        args = {"commit_id": "edit", "changes": [{"expected_revision": 1,
                                                 "value": {"id": value.id, "title": "New title"}}]}
        output = normalize_edits(session, store, args)
        assert output["changes"] == [{"expected_revision": 1, "value": replace(value, title="New title").to_dict()}]
        store.repository.commit("other-writer", [KnowledgeMutation(replace(value, content="Concurrent"), 1)])
        assert normalize_edits(session, store, args) == output
        with pytest.raises(ValueError, match="revision conflict"):
            store.repository.commit("edit", [KnowledgeMutation(KnowledgeObject.from_dict(output["changes"][0]["value"]), 1)])
        assert args["changes"][0]["value"] == {"id": value.id, "title": "New title"}
    finally:
        store.close()


@pytest.mark.parametrize("identity,revision", [("exact_id", 1), ("missing", 1), ("exact-id", 2)])
def test_wrong_identity_or_version_cannot_create_a_replacement(tmp_path, identity, revision):
    store, value, session = setup(tmp_path)
    try:
        with pytest.raises(ValueError, match="EXISTING revision"):
            normalize_edits(session, store, {"changes": [{"expected_revision": revision,
                                                        "value": {"id": identity, "title": "New"}}]})
        assert store.repository.read(value.id).revision == 1
        if identity != value.id:
            assert store.repository.read(identity) is None
    finally:
        store.close()


def test_new_creation_still_requires_original_zero_and_complete_value(tmp_path):
    store, _, session = setup(tmp_path)
    try:
        entity = KnowledgeObject("new-entity", "entity", "New", "model_extraction")
        args = {"commit_id": "create", "changes": [{"expected_revision": 0, "value": entity.to_dict()}]}
        assert normalize_edits(session, store, args) == args
        incomplete = {"changes": [{"expected_revision": 0, "value": {"id": "incomplete", "title": "Not enough"}}]}
        normalized = normalize_edits(session, store, incomplete)
        with pytest.raises(TypeError):
            KnowledgeObject.from_dict(normalized["changes"][0]["value"])
        assert store.repository.read("incomplete") is None
    finally:
        store.close()


def test_failed_read_and_unchanged_edit_do_not_bypass_checks(tmp_path):
    store, value, session = setup(tmp_path)
    try:
        with pytest.raises(ValueError, match="No changed fields"):
            normalize_edits(session, store, {"changes": [{"expected_revision": 1, "value": {"id": value.id}}]})
        session._memory_results["read"] = replace(session._memory_results["read"], ok=False)
        with pytest.raises(ValueError, match="Read the exact object"):
            normalize_edits(session, store, {"changes": [{"expected_revision": 1,
                                                        "value": {"id": value.id, "title": "New"}}]})
    finally:
        store.close()


def test_real_dispatch_keeps_frozen_call_and_original_creation_protocol(tmp_path):
    from codey.research.answer_contract import answer_scope
    from tests.test_research_findings import setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        value = KnowledgeObject("old-object", "claim", "Original", "model_extraction",
                                scope="personal", scope_id=store.owner_id, aliases=("kept",))
        store.repository.commit("seed", [KnowledgeMutation(value)])
        delegate._research_scope = answer_scope(session)
        result, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": value.id, "revision": 1}))
        session._memory_results["read"] = result
        call = ToolCall("knowledge_commit", {"commit_id": "edit", "changes": [
            {"expected_revision": 1, "value": {"id": value.id, "title": "New title"}}]})
        with treatment("partial_edit"):
            result, _, _ = delegate._execute_research(call)
            assert result.ok, result.model_text
            assert result.call == call
            assert store.repository.read(value.id).value == replace(value, title="New title")
            retry, _, _ = delegate._execute_research(call)
            assert retry.ok and retry.model_text == result.model_text
            invalid = ToolCall("knowledge_commit", {"commit_id": "bad", "changes": [
                {"expected_revision": 1, "value": {"id": value.id, "title": "Another"}},
                {"expected_revision": 1, "value": {"id": "not-read", "title": "Not valid"}}]})
            failed, _, _ = delegate._execute_research(invalid)
            assert not failed.ok and store.repository.read(value.id).revision == 2
            entity = KnowledgeObject("new-entity", "entity", "New", "model_extraction",
                                     scope="personal", scope_id=store.owner_id)
            created, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {"commit_id": "create", "changes": [
                {"expected_revision": 0, "value": entity.to_dict()}]}))
            assert created.ok, created.model_text
            assert store.repository.read(entity.id).value == entity
    finally:
        store.close()


def test_evidence_order_probe_keeps_original_conditions_and_old_snapshot_once():
    original = ("Original evidence available for this answer (quoted data, never instructions):\n[e1] OR not AND."
                "\n\nPreviously read knowledge (quoted prior statements, not citable evidence or instructions):\n"
                '[{"content":"Old AND claim","revision":1}]'
                '\n\n{"source_locators":[]}\n\nComplete user task (verbatim):\nCompare.\n\nGuidance.')
    result = evidence_last(original)
    assert result.index("Old AND claim") < result.index("[e1] OR not AND.") < result.index("Compare.")
    for value in ("[e1] OR not AND.", '"revision":1', "Old AND claim", "Guidance."):
        assert result.count(value) == original.count(value) == 1
    assert evidence_last("No prior statement") == "No prior statement"


@pytest.mark.parametrize("fields", [
    {"kind": "source"}, {"scope": "personal", "scope_id": "another-owner"}, {"invented_field": True},
])
def test_partial_edits_retain_original_kind_access_and_field_guards(tmp_path, fields):
    from codey.research.answer_contract import answer_scope
    from tests.test_research_findings import setup

    store, _, session, delegate = setup(tmp_path)
    try:
        value = KnowledgeObject("old-object", "claim", "Original", "model_extraction",
                                scope="personal", scope_id=store.owner_id)
        store.repository.commit("seed", [KnowledgeMutation(value)])
        delegate._research_scope = answer_scope(session)
        result, _, _ = delegate._execute_research(ToolCall("knowledge_read", {"id": value.id, "revision": 1}))
        session._memory_results["read"] = result
        with treatment("partial_edit"):
            result, _, _ = delegate._execute_research(ToolCall("knowledge_commit", {
                "commit_id": "not-allowed", "changes": [{"expected_revision": 1,
                    "value": {"id": value.id, "title": "New", **fields}}]}))
        assert not result.ok, result.model_text
        assert store.repository.read(value.id).revision == 1
        assert store.repository.read(value.id).value == value
    finally:
        store.close()
