"""Presentation budgets also constrain the recall introduction."""

import pytest

from codey.knowledge.model import KnowledgeObject, KnowledgeVersion
from codey.knowledge.recall import KnowledgeHit, render_recall


@pytest.mark.parametrize("compact", [False, True])
def test_recall_fits_a_budget_smaller_than_the_introduction(compact: bool) -> None:
    value = KnowledgeObject("premise", "claim", "One prior observation", "user_statement",
                            scope="session", scope_id="session")
    hits = [KnowledgeHit(KnowledgeVersion(value, 1, "", "commit"), "text match")]

    assert render_recall(hits, max_chars=1, compact=compact) == ""
    assert '"id":"premise"' in render_recall(hits, max_chars=2000, compact=compact)
