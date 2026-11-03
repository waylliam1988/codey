"""Validate the experiment's evidence and edit boundaries before real inference."""

import json
from copy import deepcopy

import pytest

from codey.research import answer_context, answer_contract, tools
from tests.manual.research_branch_review import (
    ARMS,
    ablation,
    edit_template,
    legacy_rewrite,
    recall_with_requests,
    rewrite_view,
)


@pytest.mark.parametrize("arm", ARMS)
def test_presentation_trials_restore_owners_and_never_replace_publication(arm):
    from codey.knowledge.store import KnowledgeStore
    from codey.operations.research_publication import publish_synthesis

    owners = ((answer_contract, "ANSWER_GUIDANCE"), (answer_context, "answer_context"),
              (tools.ResearchTools, "knowledge_read"), (answer_contract, "submit_answer"),
              (answer_contract, "answer_spec"), (KnowledgeStore, "publish_note"))
    before = [getattr(owner, name) for owner, name in owners]
    with ablation(arm):
        assert KnowledgeStore.publish_note is before[-1]
        assert callable(publish_synthesis)
        if arm not in {"no_state_field"}:
            assert answer_contract.submit_answer is before[3]
    assert [getattr(owner, name) for owner, name in owners] == before


def test_navigation_keeps_exact_historical_revision_and_recovery_action():
    row = {"investigation_attempts": [], "latest_error": [], "omitted_windows": [
        {"label": "e1", "recover": {"tool": "read_tool_result", "args": {"result_ref": "r1"}}}],
        "read_receipts": [{"result_ref": "r1", "tool": "knowledge_read", "passage_refs": ["p1"]}],
        "source_locators": [{"knowledge_source": ["source-exact", 1], "url": "https://example.org/a",
            "available_length": 20000, "next_offset": 5000, "sections": [{"label": "B", "offset": 15000}]}]}
    modified = json.loads(rewrite_view("\n" + json.dumps(row, sort_keys=True), "compact_navigation"))
    assert modified["source_locators"][0]["next_read"] == {
        "tool": "knowledge_read", "args": {"id": "source-exact", "revision": 1, "offset": 5000}}
    assert modified["omitted_windows"] == row["omitted_windows"]
    assert modified["source_locators"][0]["sections"] == row["source_locators"][0]["sections"]


def test_removal_really_changes_a_block_after_a_trailing_passage_newline():
    row = {"investigation_attempts": [], "source_locators": [{"url": "https://example.org/a"}],
           "omitted_windows": [{"label": "e1"}], "latest_error": []}
    view = "[e1] passage\n\nText ending in newline.\n\n\n" + json.dumps(row, sort_keys=True)
    result = legacy_rewrite(view, fields=("source_locators", "omitted_windows"))
    assert '"source_locators":[]' in result and '"omitted_windows":[]' in result
    assert "Text ending in newline." in result


def test_revision_template_preserves_all_value_fields_and_does_not_commit():
    from codey.knowledge.model import KnowledgeObject

    value = KnowledgeObject("claim-exact", "claim", "Preserve conditions", "model_extraction",
                            title="Old", status="candidate", scope="personal", scope_id="owner-exact")
    payload = {**value.to_dict(), "revision": 1, "commit_id": "original", "recorded_at": "old"}
    original = deepcopy(payload)
    template = edit_template(payload)["edit_template_not_executed"]
    assert template["args"]["changes"] == [{"value": value.to_dict(), "expected_revision": 1}]
    assert KnowledgeObject.from_dict(template["args"]["changes"][0]["value"]) == value
    assert all(payload[key] == val for key, val in original.items())
    assert "knowledge_updates" in payload["edit_instruction"]


def test_exact_read_requests_never_normalize_ids_or_change_revision():
    row = {"id": "claim-hyphen-not_underscore", "revision": 3, "kind": "claim"}
    modified = json.loads(recall_with_requests(json.dumps(row)))
    assert modified["read_request"] == {"tool": "knowledge_read", "args": {"id": row["id"], "revision": 3}}


def test_compact_guidance_preserves_protocol_accumulation_and_state_rules():
    original = answer_contract.ANSWER_GUIDANCE
    with ablation("compact_semantic"):
        assert answer_contract.ANSWER_GUIDANCE.count("Use state=answered") == 1
        assert answer_contract.ANSWER_GUIDANCE.split("Use state=answered", 1)[1] == original.split(
            "Use state=answered", 1)[1]
        assert "OR alternatives" in answer_contract.ANSWER_GUIDANCE
