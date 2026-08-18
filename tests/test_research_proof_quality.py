from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.research.ledger import ResearchLedger
from codey.research.object_model import (
    EvidenceLocator,
    ResearchClaim,
    ResearchClaimRelation,
    ResearchEvidence,
    ResearchQuestion,
    ResearchRecord,
    ResearchSource,
    build_research_record,
)
from codey.research.proof_quality import review_research_proof
from codey.research.report_quality import review_report_quality


def _report(
    url: str = "https://example.com/helium",
    *,
    conclusion: str = "Helium supply depends on gas processing.",
    evidence_line: str = "The opened source says helium is separated from natural gas streams.",
) -> str:
    return (
        "## 结论\n"
        f"- {conclusion} [1]\n\n"
        "## 关键证据\n"
        f"- [1] {evidence_line}\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新供应数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium supply\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )


def _record(
    *,
    question: str = "Research helium supply",
    summary: str | None = None,
    url: str = "https://example.com/helium",
    project: str | Path | None = None,
) -> ResearchRecord:
    ledger = ResearchLedger()
    source_text = "Helium is separated from natural gas streams. 2026 supply note."
    ledger.record_search("helium supply", [{
        "title": "Helium article",
        "url": url,
        "snippet": "Search result is not evidence.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Helium article",
        text=source_text,
    )
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    rendered = summary or _report(url)
    report_review = review_report_quality(
        rendered,
        ledger=ledger,
        opened_sources=ledger.final_url_set(),
        search_result_urls={url},
    )
    assert report_review.ok
    return build_research_record(
        question=question,
        summary=rendered,
        ledger=ledger,
        review=report_review,
        run_id="run-proof",
        session_id="session-proof",
        project=project,
        synthesis_id="synth-proof",
        stop_reason="done",
    )


def _ledger_payload(record: ResearchRecord, *, project: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as td:
        store = EvidenceLedgerStore(Path(td) / "state")
        result = store.append_record(
            record,
            run_id="run-proof",
            session_id="session-proof",
            project=project,
        )
        assert result.ok
        snapshot = store.load(session_id="session-proof", project=project)
        assert snapshot.available
        return dict(snapshot.payload)


def test_answered_cited_supported_record_gets_stable_research_proof() -> None:
    record = _record()
    ledger = _ledger_payload(record)

    first = review_research_proof(
        record,
        question="Research helium supply",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )
    second = review_research_proof(
        record,
        question="Research helium supply",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )
    different_question = review_research_proof(
        record,
        question="Find helium supply",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )

    assert first.ok is True
    assert first.answers_question is True
    assert first.answer_coverage_score == 1.0
    assert different_question.ok is True
    assert different_question.answer_coverage_score == first.answer_coverage_score
    assert first.citation_present is True
    assert first.citation_locator_verified is True
    assert first.support_relation_verified is True
    assert first.ledger_record_verified is True
    assert first.question_digest.startswith("sha256:")
    assert different_question.question_digest != first.question_digest
    assert first.proof_ref.startswith("research_proof:")
    assert first.proof_ref == second.proof_ref
    assert different_question.proof_ref != first.proof_ref


def test_unmatched_cited_claim_is_not_supported_and_emits_planner_signal() -> None:
    summary = _report(
        conclusion=(
            "Helium supply depends on gas processing. [1]\n"
            "- Helium prices will double next month. [1]"
        )
    )
    record = _record(question="Research helium price outlook", summary=summary)
    ledger = _ledger_payload(record)

    review = review_research_proof(
        record,
        question="Research helium price outlook",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )

    assert review.ok is False
    assert "unsupported_claims" in review.missing_evidence
    assert "claim_missing_support_relation" in review.missing_evidence
    assert review.followup_questions
    assert review.query_rewrite_candidates


def test_refuting_relation_does_not_support_conclusion_claim() -> None:
    source = ResearchSource(
        source_id="source:0000000000000001",
        final_url_ref={"url_digest": "sha256:" + "1" * 64, "host": "example.com"},
        title_digest="sha256:" + "2" * 64,
        content_hash="1" * 16,
        quality={"level": "primary", "kind": "official", "freshness": "fresh"},
    )
    evidence = ResearchEvidence(
        evidence_id="evidence:0000000000000001",
        source_id=source.source_id,
        excerpt_digest="sha256:" + "3" * 64,
        bounded_excerpt="Helium supply does not depend on gas processing.",
        locator=EvidenceLocator(kind="html", source_id=source.source_id, char_start=0, char_end=20),
        stance="contradicts",
        claim_text_digest="sha256:" + "4" * 64,
    )
    claim = ResearchClaim(
        claim_id="claim:0000000000000001",
        claim_text="Helium supply depends on gas processing.",
        claim_section="conclusion",
        citation_numbers=(1,),
        evidence_refs=(evidence.evidence_id,),
        status="evidence_backed",
    )
    record = ResearchRecord(
        record_id="research_record:" + "a" * 16,
        record_digest="sha256:" + "a" * 64,
        question=ResearchQuestion(
            question_id="question:" + "b" * 16,
            question_text_digest="sha256:" + "b" * 64,
            chars=22,
        ),
        answer_status="answered",
        sources=(source,),
        evidence=(evidence,),
        claims=(claim,),
        relations=(ResearchClaimRelation(
            relation_id="relation:0000000000000001",
            relation_kind="refutes",
            from_ref=claim.claim_id,
            to_ref=evidence.evidence_id,
            citation_numbers=(1,),
        ),),
        unsupported_claim_count=0,
        run_id="run-proof",
        session_id="session-proof",
        stop_reason="done",
    )
    ledger = _ledger_payload(record)

    review = review_research_proof(
        record,
        question="Research helium supply",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )

    assert review.ok is False
    assert review.support_relation_verified is False
    assert "claim_missing_support_relation" in review.missing_evidence


def test_support_relation_must_target_claim_evidence_ref_and_evidence_backed_status() -> None:
    record = _record()
    ledger = _ledger_payload(record)
    conclusion = next(claim for claim in record.claims if claim.claim_section == "conclusion")
    malformed_claim = replace(conclusion, evidence_refs=(), status="unsupported")
    malformed = replace(
        record,
        claims=tuple(malformed_claim if claim.claim_id == conclusion.claim_id else claim for claim in record.claims),
        unsupported_claim_count=0,
    )

    review = review_research_proof(
        malformed,
        question="Research helium supply",
        evidence_ledger=ledger,
        require_ledger_record=False,
    )

    assert review.ok is False
    assert review.support_relation_verified is False
    assert "claim_missing_evidence_ref" in review.missing_evidence
    assert "claim_not_evidence_backed" in review.missing_evidence
    assert "support_relation_not_claim_evidence" in review.missing_evidence


def test_counterevidence_or_limitations_check_is_required_for_ok_review() -> None:
    record = _record()
    without_counter_claims = tuple(
        claim for claim in record.claims if claim.claim_section != "counter"
    )
    retained_claim_ids = {claim.claim_id for claim in without_counter_claims}
    malformed = replace(
        record,
        claims=without_counter_claims,
        relations=tuple(
            relation for relation in record.relations if relation.from_ref in retained_claim_ids
        ),
        unsupported_claim_count=0,
    )

    review = review_research_proof(
        malformed,
        question="Research helium supply",
        evidence_ledger=None,
        require_ledger_record=False,
    )

    assert review.ok is False
    assert review.counterevidence_checked is False
    assert "counterevidence_not_checked" in review.missing_evidence


def test_missing_ledger_record_blocks_queue_proof() -> None:
    record = _record()

    review = review_research_proof(
        record,
        question="Research helium supply",
        evidence_ledger=None,
        require_ledger_record=True,
    )

    assert review.ok is False
    assert review.ledger_record_verified is False
    assert "missing_evidence_ledger_record" in review.missing_evidence


def test_planner_signals_do_not_echo_raw_url_path_or_secret_terms() -> None:
    record = _record(
        question=(
            "Research SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX and "
            "https://example.com/items?token=SECRET and E:/secret/project"
        )
    )
    ledger = _ledger_payload(record)

    review = review_research_proof(
        record,
        question=(
            "Research SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX and "
            "https://example.com/items?token=SECRET and E:/secret/project"
        ),
        evidence_ledger=ledger,
        require_ledger_record=True,
    )
    serialized = json.dumps(review.to_payload(), ensure_ascii=False)

    assert "SECRET_TOKEN" not in serialized
    assert "https://example.com" not in serialized
    assert "E:/secret/project" not in serialized


def test_planner_signals_keep_secreted_science_terms() -> None:
    record = _record(question="secreted insulin secretion pathway")
    ledger = _ledger_payload(record)

    review = review_research_proof(
        record,
        question="secreted insulin secretion pathway",
        evidence_ledger=ledger,
        require_ledger_record=True,
    )
    serialized = json.dumps(review.to_payload(), ensure_ascii=False)

    assert "secreted insulin secretion pathway" in serialized


def test_assumption_claim_does_not_count_as_supported_answer() -> None:
    record = _record()
    conclusion = next(claim for claim in record.claims if claim.claim_section == "conclusion")
    assumption_claim = replace(
        conclusion,
        evidence_refs=(),
        assumption_refs=("assumption:0000000000000001",),
        status="assumption",
    )
    malformed = replace(
        record,
        claims=(assumption_claim,),
        evidence=(),
        relations=(),
        unsupported_claim_count=0,
    )

    review = review_research_proof(
        malformed,
        question="Research helium supply",
        evidence_ledger=None,
        require_ledger_record=False,
    )

    assert review.ok is False
    assert "assumption_used_as_answer" in review.missing_evidence
