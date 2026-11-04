"""Each prompt candidate changes one display concern and restores production."""

import json
from dataclasses import fields

import pytest

from codey.knowledge.recall import render_recall
from codey.operations import knowledge_context
from codey.research import answer_contract, methods
from codey.toolchain.tool_spec import ToolSpec
from tests.manual import research_prompt_followups_ab as experiment
from tests.test_research_read_guidance_ab import hits


def test_read_at_use_removes_only_schema_reminder_and_preserves_trust_in_read_result():
    scope = answer_contract.AnswerScope("a" * 64, (), ())
    spec = ToolSpec("knowledge_read", parameters=(("id", {"type": "string"}),), required=("id",),
                    description="Read an exact stored object.")
    original = answer_contract.answer_spec(spec, scope)
    reminder = answer_contract.KNOWLEDGE_READ_GUIDANCE
    with experiment.treatment("read_at_use"):
        current = answer_contract.answer_spec(spec, scope)
        assert current.description + " " + reminder == original.description
        assert all(getattr(current, field.name) == getattr(original, field.name)
                   for field in fields(current) if field.name != "description")
        assert reminder == answer_contract.KNOWLEDGE_READ_GUIDANCE
    assert answer_contract.answer_spec(spec, scope) == original


@pytest.mark.parametrize("budget", [900, 1500, 4000])
def test_recall_header_keeps_identical_admitted_rows_and_read_reminder(budget):
    original = render_recall(hits(), max_chars=budget, compact=True, include_answer_refs=False)
    reminder = answer_contract.KNOWLEDGE_READ_GUIDANCE
    with experiment.treatment("recall_header"):
        current = knowledge_context.render_recall(hits(), max_chars=budget, compact=True, include_answer_refs=False)
        assert [json.loads(x) for x in original.splitlines() if x.startswith("{")] == [
            json.loads(x) for x in current.splitlines() if x.startswith("{")]
        assert len(current) < len(original)
        assert reminder == answer_contract.KNOWLEDGE_READ_GUIDANCE


def test_unread_action_uses_exact_stored_version_and_never_points_to_eof():
    view = {"investigation_attempts": [], "source_locators": [
        {"knowledge_source": ["source-pinned", 2], "available_length": 900, "next_offset": 500},
        {"url": "https://example.com/read", "available_length": 800, "next_offset": 800},
        {"url": "https://example.com/read2", "available_length": 800, "next_offset": 300}]}
    actual = experiment.rewrite_navigation(json.dumps(view, sort_keys=True))
    rows = json.JSONDecoder().raw_decode(actual)[0]["source_locators"]
    assert rows[0]["next_read"] == {"tool": "knowledge_read", "args": {
        "id": "source-pinned", "revision": 2, "offset": 500}}
    assert "next_read" not in rows[1]
    assert rows[2]["next_read"]["args"]["offset"] == 300


def test_methods_short_preserves_domain_sources_conditions_and_clinical_boundary():
    before = methods.research_method_guidance("sqlite cancer clinical treatment", evidence_only=True)
    with experiment.treatment("methods_short"):
        after = methods.research_method_guidance("sqlite cancer clinical treatment", evidence_only=True)
        assert len(after) < len(before)
        assert "version, environment, workload, reproduction" in after
        assert "in vitro/animal/human distinctions" in after
        assert "never infer individual treatment" in after
    assert methods.research_method_guidance("sqlite cancer clinical treatment", evidence_only=True) == before
