"""Quote repair can inspect original text without silently saving a replacement."""

from codey.research.ledger import ResearchLedger

URL = "https://example.com/writers"
ORIGINAL = "但 SQLite 仍遵循單寫入者模型，只有一條 writer path，長交易或多 process 寫入都會在這條 path 上排隊。"


def test_script_conversion_is_rejected_with_original_passage_and_no_evidence():
    ledger = ResearchLedger()
    ledger.record_open(requested_url=URL, final_url=URL, title="Writer model",
                       text="Introduction.\n\n" + ORIGINAL + "\n\nOther details.")
    proposed = ORIGINAL.replace("單寫入者", "单写入者").replace("一條", "一条").replace("長交易", "长交易")
    prepared = ledger.prepare_evidence_items([{"claim": "There is one writer", "source_url": URL,
        "excerpt": proposed}], fallback_sources=[URL], fallback_claim="Writer", fallback_body="Writer", note_type="fact")
    assert prepared.error and not prepared.items and not ledger.evidence_items
    assert "not saved and not verified to support your claim" in prepared.error
    assert ORIGINAL in prepared.error and "offset=15" in prepared.error
    assert "Do not translate" in prepared.error
    accepted = ledger.prepare_evidence_items([{"claim": "There is one writer", "source_url": URL,
        "excerpt": ORIGINAL}], fallback_sources=[URL], fallback_claim="Writer", fallback_body="Writer", note_type="fact")
    assert not accepted.error and accepted.items[0].excerpt == ORIGINAL


def test_unrelated_quote_has_no_fabricated_or_weak_match_hint():
    ledger = ResearchLedger()
    ledger.record_open(requested_url=URL, final_url=URL, title="Writers", text=ORIGINAL)
    assert ledger._rejected_excerpt_hint(URL, "Unrelated vaccine efficacy in a randomized trial.", page=None) == ""
    assert ledger._rejected_excerpt_hint(URL, "x" * 1201, page=None) == ""
