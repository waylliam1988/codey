"""Semantic wording experiments must not change publication or knowledge guards."""

import pytest

from codey.research import answer_context, answer_contract
from tests.manual import research_guidance_simplification_ab as experiment


@pytest.mark.parametrize("arm", experiment.ARMS)
def test_wording_preserves_the_submission_and_storage_contract(arm):
    original = answer_contract.ANSWER_GUIDANCE
    suffix = original[original.index("Use state=answered"):]
    spec = answer_contract.answer_spec
    scope = answer_contract.answer_scope
    tools = answer_contract.ANSWER_TOOLS
    with experiment.treatment(arm, reference=original):
        current = answer_contract.ANSWER_GUIDANCE
        assert current[current.index("Use state=answered"):] == suffix
        assert current == answer_context.ANSWER_GUIDANCE
        assert answer_contract.answer_spec is spec
        assert answer_contract.answer_scope is scope
        assert tools == answer_contract.ANSWER_TOOLS
    assert original == answer_context.ANSWER_GUIDANCE
    assert original == answer_contract.ANSWER_GUIDANCE


def test_compact_wording_has_no_case_answers_and_really_reduces_text():
    original = answer_contract.ANSWER_GUIDANCE
    short = experiment.compact_guidance(original)
    assert len(short) < len(original)
    assert all(value not in short for value in ("3.22.0", "C1", "C2", "license", "ACTIVE"))
    for value in ("version", "setting", "conditions", "exceptions", "AND/OR", "uncertainty", "[eN]"):
        assert value in short


def test_invalid_arm_is_rejected():
    with pytest.raises(ValueError), experiment.treatment("unknown"):
        pass
