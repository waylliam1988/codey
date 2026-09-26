"""PLR split B4: preservation + deterministic bug hunts (no full pytest)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research import record_merge as rm
from codey.research.evidence_ledger import EvidenceLedgerStore, _canonical_ledger_payload
from codey.research.ledger import ResearchLedger
from codey.research.object_model import build_research_record
from codey.research.proof_quality import _overclaim_warnings, _review_relations
from codey.research.report_quality import review_report_quality
from codey.research.source_document import SourceDocument
from codey.research.tools import ResearchTools


class _DummySearch:
    def close(self) -> None:
        pass


def _ledger_with_one_source() -> ResearchLedger:
    ledger = ResearchLedger()
    url = "https://example.com/plr-b4"
    ledger.record_open_document(SourceDocument.html(
        requested_url=url,
        final_url=url,
        title="PLR B4 Source",
        text="PLR B4 verified fact text.",
    ))
    return ledger


def test_canonical_split_preserves_valid_ledger() -> None:
    ledger = ResearchLedger()
    url = "https://example.com/plr-b4-ledger"
    ledger.record_search("plr b4", [{
        "title": "PLR B4",
        "url": url,
        "snippet": "snippet",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="PLR B4",
        text="PLR B4 verified fact text 2026.",
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "PLR B4 claim.",
            "source_url": url,
            "excerpt": "PLR B4 verified fact text.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="PLR B4 claim.",
        fallback_body="PLR B4 verified fact text 2026.",
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-plr-b4")
    rendered = (
        "## 结论\n- PLR B4 claim. [1]\n\n"
        "## 关键证据\n- [1] PLR B4 verified fact text.\n\n"
        "## 反证与限制\n- 未找到强反证。\n\n"
        "## 来源质量\n- [1] secondary.\n\n"
        "## 搜索覆盖\n- query: plr b4\n\n"
        f"## 来源\n[1] PLR B4 - {url}"
    )
    review = review_report_quality(
        rendered,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert review.ok
    record = build_research_record(
        question="PLR B4 question",
        summary=rendered,
        ledger=ledger,
        review=review,
        run_id="run-plr-b4",
        session_id="session-plr-b4",
        stop_reason="done",
    )
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(record, run_id="run-plr-b4", session_id="session-plr-b4")
        assert result.ok
        snapshot = store.load(session_id="session-plr-b4")
        assert snapshot.available
        assert _canonical_ledger_payload(dict(snapshot.payload)) is True


def test_inject_split_preserves_sections() -> None:
    ledger = _ledger_with_one_source()
    url = "https://example.com/plr-b4"
    sections = {
        "conclusion": "",
        "evidence": "",
        "counter": "",
        "source_quality": "",
        "coverage": "",
        "sources": "",
    }
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "PLR B4 claim.",
            "source_url": url,
            "excerpt": "PLR B4 verified fact text.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="PLR B4 claim.",
        fallback_body="PLR B4 verified fact text.",
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-plr-b4")
    updated = rm._inject_new_evidence_into_sections(
        sections,
        list(ledger.evidence_items),
        ledger,
        rebuild=True,
    )
    assert url in updated["sources"]
    assert updated["evidence"].strip() != ""


def test_review_split_preserves_verdict_shape() -> None:
    verdict = _review_relations(
        claims={},
        evidence={},
        assumptions={},
        relations=(),
        sources={},
    )
    assert verdict["citation_present"] is False
    assert verdict["support_relation_verified"] is False
    assert verdict["hard_failures"] == ()
    assert verdict["diagnostics"] == ()


def test_knowledge_write_split_preserves_save() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        root = Path(td)
        store = KnowledgeStore(root / "knowledge")
        try:
            tools = ResearchTools(
                search=_DummySearch(),
                store=store,
                changes=KnowledgeChanges(root=store.root),
                session_id="session-plr-b4",
                project="project-plr-b4",
            )
            url = "https://example.com/plr-b4-write"
            tools.sources_read.add(url)
            tools.ledger.record_open_document(SourceDocument.html(
                requested_url=url,
                final_url=url,
                title="PLR B4 Write Source",
                text="PLR B4 write verified fact.",
            ))
            out = tools.knowledge_write({
                "type": "fact",
                "title": "PLR B4 Write Fact",
                "body": "PLR B4 write verified fact.",
                "sources": [url],
            })
            assert out.startswith("saved fact note id=")
        finally:
            store.index.close()


def test_render_search_coverage_none_queries_graceful() -> None:
    ledger = MagicMock()
    ledger.coverage_payload.return_value = {
        "queries": None,
        "opened_count": 0,
        "skipped_results": None,
    }
    ledger.final_url_set.return_value = set()
    ledger.evidence_items = []
    text = rm._render_search_coverage(ledger, set())
    assert "已打开来源: 0" in text
    assert "证据条目: 0" in text


def test_overclaim_accepts_set_like_frozenset() -> None:
    claims = {
        "claim:0000000000000001": {
            "claim_section": "conclusion",
            "claim_text": "This always works and is guaranteed.",
        },
    }
    from_frozen = _overclaim_warnings(claims, frozenset({"claim:0000000000000001"}))
    from_set = _overclaim_warnings(claims, set({"claim:0000000000000001"}))  # type: ignore[arg-type]
    assert from_frozen == ()
    assert from_set == ()
