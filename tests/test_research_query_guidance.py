"""The production Research prompt includes the validated query guidance."""
from __future__ import annotations

from codey.research.answer_contract import ANSWER_GUIDANCE, QUERY_GUIDANCE


def test_production_research_prompt_contains_complete_query_guidance_once() -> None:
    assert QUERY_GUIDANCE == (
        "Form each search around the exact subject, requested version, operation and missing condition. "
        "Use the source's original terminology and a short alternate-language query when the wording differs. "
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    )
    assert ANSWER_GUIDANCE.count(QUERY_GUIDANCE) == 1
