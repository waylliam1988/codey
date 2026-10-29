"""A located heading cannot stand in for the result in its section body."""

import pytest

from codey.research.ledger import ResearchLedger

URL = "https://example.com/wal"
FINDING = "A long-running read transaction can prevent a checkpointer from making progress."
HEADING = "Sometimes Queries Return SQLITE_BUSY In WAL Mode"


@pytest.mark.parametrize("quote", [HEADING, "9. " + HEADING, "Write-Ahead Logging"])
def test_label_cannot_be_saved_as_support_for_a_different_finding(quote):
    ledger = ResearchLedger()
    ledger.record_open(requested_url=URL, final_url=URL, title="Write-Ahead Logging",
                       text=f"Write-Ahead Logging\n{HEADING}\n\n## 9. {HEADING}\n\n{FINDING}")
    prepared = ledger.prepare_evidence_items([
        {"claim": FINDING, "source_url": URL, "excerpt": FINDING},
        {"claim": FINDING, "source_url": URL, "excerpt": quote},
    ], fallback_sources=[URL], fallback_claim=FINDING, fallback_body=FINDING, note_type="fact")
    assert "quotes only a source title or section heading" in prepared.error
    assert not prepared.items and not ledger.evidence_items


def test_body_passage_remains_usable_and_source_catalogue_can_quote_title():
    ledger = ResearchLedger()
    title = "Write-Ahead Logging"
    ledger.record_open(requested_url=URL, final_url=URL, title=title,
                       text=f"{title}\n\n## 9. {HEADING}\n\n{FINDING}")
    for kind, quote in (("fact", FINDING), ("source", title)):
        prepared = ledger.prepare_evidence_items(
            [{"claim": FINDING, "source_url": URL, "excerpt": quote}],
            fallback_sources=[URL], fallback_claim=FINDING, fallback_body=FINDING, note_type=kind)
        assert not prepared.error and len(prepared.items) == 1
