"""Experimental wording stays isolated from production and source acquisition."""

import json

import pytest

from codey.research import answer_context, answer_contract
from tests.manual import research_fidelity_source_ab as experiment


@pytest.mark.parametrize("arm", experiment.ARMS)
def test_only_experimental_guidance_changes_and_is_restored(arm):
    original = answer_contract.ANSWER_GUIDANCE
    renderer_original = answer_context.ANSWER_GUIDANCE
    original_spec = answer_contract.answer_spec
    original_tools = answer_contract.ANSWER_TOOLS
    original_scope = answer_contract.answer_scope
    original_submit = answer_contract.submit_answer
    with experiment.treatment(arm):
        expected = original + ("\n" + experiment._guidance(arm) if arm != "baseline" else "")
        assert expected == answer_contract.ANSWER_GUIDANCE
        assert expected == answer_context.ANSWER_GUIDANCE
        assert answer_contract.answer_spec is original_spec
        assert original_tools == answer_contract.ANSWER_TOOLS
        assert answer_contract.answer_scope is original_scope
        assert answer_contract.submit_answer is original_submit
    assert original == answer_contract.ANSWER_GUIDANCE
    assert renderer_original == answer_context.ANSWER_GUIDANCE


def test_generic_rules_do_not_supply_the_old_case_answer_or_forbid_discovery():
    assert "license" not in experiment.FAITHFUL_GUIDANCE
    assert "allowance" not in experiment.FAITHFUL_GUIDANCE
    assert "C1" not in experiment.FAITHFUL_GUIDANCE
    assert "AND/OR" in experiment.FAITHFUL_GUIDANCE
    assert "not stated" in experiment.FAITHFUL_GUIDANCE
    assert "source discovery" in experiment.DIRECT_SOURCE_GUIDANCE
    assert "direct reading fails" in experiment.DIRECT_SOURCE_GUIDANCE


def test_discovery_fixture_keeps_the_source_unknown_and_knowledge_empty(tmp_path):
    previous = tmp_path / "previous"
    previous.mkdir()
    docs = [{"url": "https://example.org/source", "title": "fixture", "text": "Frozen original."}]
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json"):
        (previous / name).write_text("{}", encoding="utf8")
    for name in ("empty-knowledge", "warm-knowledge"):
        (previous / name).mkdir()
    cases = [{"id": name, "question": "Read source.", "documents": docs}
             for name in ("error_complex", "two_errors", "accumulate")]
    (previous / "fixtures.json").write_text(json.dumps({"cases": cases}), encoding="utf8")
    root = tmp_path / "experiment"
    experiment.prepare(root, previous)
    fixtures = json.loads((root / "fixtures.json").read_text(encoding="utf8"))["cases"]
    assert [case["id"] for case in fixtures] == list(experiment.CASES)
    discovery = next(case for case in fixtures if case["id"] == "source_discovery")
    assert discovery["warm"] and discovery["knowledge_seed"] == "empty-knowledge"
    assert "http" not in discovery["question"] and "搜索" in discovery["question"]
    assert discovery["documents"] == docs
