"""Experimental selectors cannot change read authority or silently split windows."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from codey.research import answer_context as production
from tests.manual import research_window_retention_candidate as candidate
from tests.manual.research_window_retention_cases import cases


def world(texts, windows=None):
    refs = [f"p{i}" for i in range(len(texts))]
    observations = windows or [{ref} for ref in refs]
    session = SimpleNamespace(task_text="Orin R2 remote export token administrator offline", _memory_results={
        f"read-{i}": SimpleNamespace(ok=True, canonical={"read_passages": [{"ref": ref} for ref in sorted(window)]})
        for i, window in enumerate(observations)})
    scope = SimpleNamespace(aliases=tuple((f"e{i + 1}", ref) for i, ref in enumerate(refs)))
    ledger = SimpleNamespace(read_passages={ref: SimpleNamespace(excerpt=text, source=SimpleNamespace(title="Source"))
                                           for ref, text in zip(refs, texts, strict=True)})
    return session, ledger, scope


def render(_session, ledger, _scope, visible):
    return "base:" + "|".join(ledger.read_passages[ref].excerpt for _, ref in visible)


def test_fitting_context_matches_production_exactly_and_does_not_rank(monkeypatch):
    session, ledger, scope = world(["Orin rule", "catalogue"])
    monkeypatch.setattr(production, "_render_context", render)
    monkeypatch.setattr(candidate, "eviction_order", lambda *_: pytest.fail("ranking a fitting view"))
    assert candidate.answer_context(session, ledger, scope, count_tokens=len, token_budget=100) == \
        production.answer_context(session, ledger, scope, count_tokens=len, token_budget=100)


def test_overflow_keeps_early_relevant_window_without_mutating_authority(monkeypatch):
    session, ledger, scope = world(["Orin R2 export token administrator offline", "later catalogue " * 2,
                                   "unrelated archive " * 2])
    original = deepcopy(session._memory_results)
    aliases = scope.aliases
    monkeypatch.setattr(production, "_render_context", render)
    baseline = production.answer_context(session, ledger, scope, count_tokens=len, token_budget=90)
    selected = candidate.answer_context(session, ledger, scope, count_tokens=len, token_budget=90)
    assert "administrator" not in baseline and "administrator" in selected
    assert len(selected) <= 90
    assert scope.aliases == aliases and session._memory_results == original


def test_overlapping_observations_are_one_atomic_window():
    session, _ledger, scope = world(["first", "middle", "last"], [{"p0", "p1"}, {"p1", "p2"}])
    assert candidate.groups(session, scope) == [{"p0", "p1", "p2"}]


def test_duplicate_body_does_not_occupy_two_priority_slots():
    session, ledger, scope = world(["Orin export", "Orin export", "unrelated catalogue"])
    ordered = candidate.eviction_order(session, ledger, scope)
    assert ordered[0] == {"p0"}  # Older exact duplicate first; labels remain valid.
    assert len(scope.aliases) == 3


@pytest.mark.parametrize("budget", [0, -1])
def test_invalid_budget_rejected(budget):
    with pytest.raises(ValueError, match="positive"):
        candidate.answer_context(*world(["text"]), token_budget=budget)


def test_non_evidence_overflow_is_not_hidden(monkeypatch):
    monkeypatch.setattr(production, "_render_context", render)
    with pytest.raises(ValueError, match="non-evidence"):
        candidate.answer_context(*world(["text"]), count_tokens=len, token_budget=2)


def test_frozen_fixtures_have_no_scripted_reads_or_gold_in_questions():
    fixtures = cases()
    assert len([c for c in fixtures if c["split"] == "dev"]) == 2
    assert len([c for c in fixtures if c["split"] == "holdout"]) == 6
    for case in fixtures:
        assert "controlled_sequence" not in case
        assert all(any(key["quote"] in doc["text"] for doc in case["documents"]) for key in case["keys"])
        assert all(key["quote"] not in case["question"] for key in case["keys"])
