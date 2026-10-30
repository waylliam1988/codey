"""Explicit lost statistical qualifiers fail atomically, without inferring alpha."""

import pytest

from codey.research.evidence_qualifiers import support_qualifier_problem
from codey.research.ledger import ResearchLedger


@pytest.mark.parametrize("quote", [
    "Survival increased, but not statistically significant (P = 0.065).",
    "No statistically significant survival difference was observed.",
    "The survival difference was nonsignificant.",
])
@pytest.mark.parametrize("claim", [
    "The intervention significantly increased survival.",
    "该方案显著延长生存时间。",
    "The intervention had a significant benefit, particularly the alternating regimen.",
])
def test_explicit_nonsignificance_cannot_support_unqualified_significance(claim, quote):
    assert "Split different comparisons" in support_qualifier_problem(claim, quote)


@pytest.mark.parametrize("claim,quote", [
    ("The effect was not statistically significant.", "The effect was not statistically significant."),
    ("生存差异未达到统计学显著性。", "Survival increased, but not statistically significant."),
    ("单药差异不显著，交替药物的差异显著。", "Alone: not significant. Alternating: significant."),
    ("是否显著尚不明确。", "The effect was not significant."),
    ("A significant improvement was observed.", "The estimate has P = 0.065."),
    ("A significant improvement was observed.", "A significant improvement was observed."),
    ("The difference was not significant.", "No statistically significant effect was found."),
])
def test_check_keeps_denials_uncertainty_and_does_not_choose_p_value_threshold(claim, quote):
    assert not support_qualifier_problem(claim, quote)


def test_conflicting_second_item_rejects_whole_batch_but_counterevidence_keeps_stance():
    url = "https://example.org/results"
    quote = "The intervention was not statistically significant."
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Results", "One observation was measured. " + quote)
    items = [{"claim": "One observation was measured.", "source_url": url,
              "excerpt": "One observation was measured."},
             {"claim": "The intervention significantly increased survival.", "source_url": url,
              "excerpt": quote}]
    kwargs = dict(fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
    prepared = ledger.prepare_evidence_items(items, **kwargs)
    assert prepared.error and not prepared.items and not ledger.evidence_items
    items[1]["stance"] = "contradicts"
    prepared = ledger.prepare_evidence_items(items, **kwargs)
    assert not prepared.error and prepared.items[1].stance == "contradicts"


@pytest.mark.parametrize("quote", [
    "Adaptive therapy may slow or stop tumor growth.",
    "The strategy might reduce contention.",
])
def test_proposed_effect_cannot_be_saved_as_demonstrated_benefit(quote):
    assert "may/might" in support_qualifier_problem("该干预有效改善结果。", quote)


@pytest.mark.parametrize("claim,quote", [
    ("该研究评估该干预是否有效。", "The intervention may improve results."),
    ("该干预可能减缓生长，尚未证实疗效。", "Adaptive therapy may slow growth."),
    ("The trial aims to evaluate tumor control.", "Adaptive therapy may slow growth."),
    ("The intervention reduced events.", "In May 2026, the intervention reduced events."),
    ("The intervention reduced events.", "Events decreased in the trial."),
])
def test_qualifier_check_preserves_uncertainty_and_does_not_interpret_calendar_dates(claim, quote):
    assert not support_qualifier_problem(claim, quote)


def test_lost_modal_qualifier_rejects_batch_atomically_but_allows_counterevidence():
    url = "https://example.org/registration"
    quote = "The intervention may improve results."
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Study", "A trial is registered. " + quote)
    items = [{"claim": "A trial is registered.", "source_url": url, "excerpt": "A trial is registered."},
             {"claim": "该干预改善结果。", "source_url": url, "excerpt": quote}]
    kwargs = dict(fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
    prepared = ledger.prepare_evidence_items(items, **kwargs)
    assert prepared.error and not prepared.items and not ledger.evidence_items
    items[1]["stance"] = "contradicts"
    prepared = ledger.prepare_evidence_items(items, **kwargs)
    assert not prepared.error and prepared.items[1].stance == "contradicts"
