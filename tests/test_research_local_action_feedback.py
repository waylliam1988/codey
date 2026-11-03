"""State and identity repairs match the current frozen local action space."""

import json

import pytest

from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_guidance_for_policy
from codey.protocols.text_turn import decode_text_turn
from codey.research.tools import ResearchToolOutput
from codey.runtime.core.models import ToolCall
from tests.test_research_inline_submission import inline_setup


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mode", ["select", "inline"])
def test_empty_existing_selection_can_only_be_unresolved_but_inline_evidence_can_be_answered(tmp_path, native, mode):
    store, tools, session, scope = inline_setup(tmp_path, mode=mode)
    try:
        snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        spec = next(row for row in snapshot.frozen_specs if row.name == "submit_research_answer")
        states = tuple(dict(spec.parameters)["state"]["enum"])
        assert states == (("unresolved",) if mode == "select" else ("answered", "partial", "unresolved"))
        gap = normalize_turn(decode_text_turn(json.dumps({"tool": spec.name, "args": {
            "state": "unresolved", "findings": []}})), snapshot=snapshot)
        assert not gap.protocol_error
        if mode == "inline":
            result, ok, _ = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)._execute_research(
                ToolCall(spec.name, {"state": "partial", "findings": []}))
            assert not ok and "finding={claim,passage_ref,...}" in result.model_text
            assert "state=unresolved, findings=[]" in result.model_text
        else:
            invalid = normalize_turn(decode_text_turn(json.dumps({"tool": spec.name, "args": {
                "state": "partial", "findings": []}})), snapshot=snapshot)
            assert invalid.protocol_error
        assert not tools.created_ids
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("kind", ["passage", "source"])
@pytest.mark.parametrize("mode", ["select", "inline"])
def test_namespace_mistake_recovers_original_read_without_inventing_a_knowledge_id(tmp_path, kind, mode):
    store, tools, session, scope = inline_setup(tmp_path, mode=mode)
    try:
        identity = scope.passage_refs[0] if kind == "passage" else next(iter(session.source_ids))
        result, ok, _ = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)._execute_research(
            ToolCall("knowledge_read", {"id": identity, "question_ref": scope.question_ref}))
        assert not ok and "read_receipts" in result.model_text and "read_receipt_index" not in result.model_text and "read_tool_result" in result.model_text
        assert "knowledge_search" not in result.model_text  # It may be outside this request's exposed tool set.
        assert not result.canonical and not tools.created_ids
        assert len(tools.ledger.read_passages) == len(scope.passage_refs)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_legitimate_stored_id_with_same_spelling_as_source_locator_is_still_read(tmp_path, monkeypatch):
    store, tools, session, scope = inline_setup(tmp_path)
    try:
        identity = next(iter(session.source_ids))
        monkeypatch.setattr(type(tools), "knowledge_read", lambda *_a, **_k: ResearchToolOutput("Actual stored object", ok=True))
        result, ok, _ = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)._execute_research(
            ToolCall("knowledge_read", {"id": identity, "question_ref": scope.question_ref}))
        assert ok and result.model_text == "Actual stored object"
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_inline_guidance_binds_identity_without_asking_to_copy_it_into_the_finding(tmp_path):
    store, _, session, _ = inline_setup(tmp_path)
    try:
        turn = prepare_kernel_turn(session, user_task=session.task_text, context_text="", native=False,
                                  task_guidance=task_guidance_for_policy(session.policy))
        assert "Inline finding omits question_ref" in turn.prompt
        assert "the frozen scope already binds its question" in turn.prompt
        assert "read-passage pN" in turn.prompt
    finally:
        store.close()
