import pytest

from tests.manual.research_delivery_candidates import DELIVERY_PROMPT, four_b_instruction


def test_4a_prompt_requires_real_structured_save():
    assert "knowledge_updates" in DELIVERY_PROMPT
    assert "does not save" in DELIVERY_PROMPT


@pytest.mark.parametrize("arm", ["U", "R", "S", "V"])
def test_4b_has_four_distinct_frozen_treatments(arm):
    assert four_b_instruction(arm)


def test_4b_rejects_unregistered_arm():
    with pytest.raises(ValueError):
        four_b_instruction("X")
