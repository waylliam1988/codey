"""Evaluation must not leak rubrics or mistake locator checks for semantics."""

import json

import pytest

from tests.manual.research_layered_local import FIXTURE, mechanical_checks, prompt_for


@pytest.mark.parametrize("level", [1, 2, 3])
def test_fixed_prompts_do_not_include_private_rubrics(level):
    for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]:
        prompt = prompt_for(case, level)
        assert case["question"] in prompt
        assert all(row["text"] in prompt for row in case["sources"])
        assert not any(rule in prompt for rule in case["review_rubric_not_sent"])
        assert "review_rubric_not_sent" not in prompt


def test_locator_check_rejects_bad_quotes_but_cannot_certify_claim_meaning():
    case = {"sources": [{"ref": "S1", "text": "Readers and writers run concurrently."}]}
    row = {"claim": "多个写入者同时提交。", "conditions": "未知", "source_ref": "S1",
           "excerpt": case["sources"][0]["text"]}
    checks = mechanical_checks(case, 2, json.dumps({"findings": [row]}))
    assert checks["exact_excerpt_locators"]  # Wrong claim; no semantic verdict is emitted.
    assert "semantic_acceptance" not in checks
    row["excerpt"] = "Fabricated exact quote"
    assert not mechanical_checks(case, 2, json.dumps({"findings": [row]}))["exact_excerpt_locators"]
    assert not mechanical_checks(case, 2, json.dumps({"findings": []}))["findings_shape"]
    assert not mechanical_checks(case, 2, "```json\n{}\n```")["json_object"]
