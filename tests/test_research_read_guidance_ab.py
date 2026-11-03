"""Check the intervention itself before spending live-model requests."""

import json

import pytest

from codey.knowledge.model import KnowledgeObject, KnowledgeVersion
from codey.knowledge.recall import KnowledgeHit, render_recall
from codey.operations import knowledge_context
from codey.research import answer_contract
from tests.manual.research_read_guidance_ab import (
    LONG_READ,
    SHORT_READ,
    rewrite_recall,
    treatment,
)


def hits():
    return [KnowledgeHit(KnowledgeVersion(
        KnowledgeObject(f"claim-test-guidance-{i}", "claim", "content " * 100, "model_extraction"),
        1, "2026-11-03", "fixture"), "text match") for i in range(10)]


@pytest.mark.parametrize("budget", [1, 900, 1500, 2500, 4000, 6000])
@pytest.mark.parametrize("answer_refs", [False, True])
def test_short_headers_preserve_exact_admitted_rows(budget, answer_refs):
    before = render_recall(hits(), compact=True, max_chars=budget, include_answer_refs=answer_refs)
    after = rewrite_recall(before, short=True)
    assert rewrite_recall(after, short=False) == before
    assert len(after) < len(before)
    def rows(text):
        return [json.loads(line) for line in text.splitlines() if line.startswith("{")]

    assert rows(before) == rows(after)
    with treatment("compact"):
        assert knowledge_context.render_recall(hits(), compact=True, max_chars=budget,
                                               include_answer_refs=answer_refs) == after


def test_only_designated_guidance_changes_and_restores():
    original = knowledge_context.render_recall
    assert answer_contract.KNOWLEDGE_READ_GUIDANCE == LONG_READ
    whole_answer = answer_contract.ANSWER_GUIDANCE
    noncompact = render_recall(hits())
    with treatment("baseline"):
        assert knowledge_context.render_recall is original
    with treatment("compact"):
        assert answer_contract.KNOWLEDGE_READ_GUIDANCE == SHORT_READ
        assert whole_answer == answer_contract.ANSWER_GUIDANCE
        assert knowledge_context.render_recall(hits()) == noncompact
        assert knowledge_context.render_recall([], compact=True) == ""
    assert knowledge_context.render_recall is original
    assert answer_contract.KNOWLEDGE_READ_GUIDANCE == LONG_READ


def test_unknown_or_wrong_phase_is_rejected():
    with pytest.raises(ValueError), treatment("unknown"):
        pass
    with pytest.raises(ValueError), treatment("production"):
        pass
    with pytest.raises(ValueError):
        rewrite_recall("Unexpected header", short=True)
