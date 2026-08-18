from __future__ import annotations

import json
from pathlib import Path

from codey.research.query_planner import build_research_plan, research_plan_trace_payload
from codey.research.source_connectors import SourceConnectorRegistry, SourceConnectorSpec


def _review(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "proof_ref": "research_proof:" + "a" * 16,
        "question_digest": "sha256:" + "b" * 64,
        "ok": False,
        "answers_question": False,
        "answer_status": "partial",
        "coverage_gaps": [{"reason_code": "missing_question_term", "term_ref": "hepatotoxicity"}],
        "followup_questions": [
            {
                "kind": "followup_question",
                "text": "Find clinical evidence for immune checkpoint hepatotoxicity.",
                "reason_code": "coverage_gap",
            }
        ],
        "query_rewrite_candidates": [
            {
                "kind": "query_rewrite",
                "text": "immune checkpoint hepatotoxicity corticosteroids",
                "reason_code": "coverage_gap",
            }
        ],
        "missing_evidence": ["answer_coverage_gap"],
    }
    base.update(overrides)
    return base


def test_medical_question_prefers_pubmed_and_builds_stable_bounded_plan() -> None:
    first = build_research_plan(
        _review(),
        question="Research immune checkpoint inhibitor hepatotoxicity clinical management",
    )
    second = build_research_plan(
        _review(),
        question="Research immune checkpoint inhibitor hepatotoxicity clinical management",
    )

    assert first.plan_ref == second.plan_ref
    assert first.plan_ref.startswith("research_plan:")
    assert first.question_digest == "sha256:" + "b" * 64
    assert first.proof_ref == "research_proof:" + "a" * 16
    assert first.max_depth == 1
    assert first.max_queries <= 8
    assert first.max_sources <= 12
    assert first.source_preferences[0].connector_id == "pubmed"
    assert "medical_life_science_source" in first.source_preferences[0].reason_codes
    assert first.query_candidates


def test_ok_proof_without_required_gap_builds_noop_plan() -> None:
    plan = build_research_plan(
        _review(
            ok=True,
            answers_question=True,
            answer_status="answered",
            coverage_gaps=(),
            missing_evidence=(),
            followup_questions=[
                {"text": "immune checkpoint inhibitor hepatotoxicity clinical management"}
            ],
            query_rewrite_candidates=[
                {"text": "immune checkpoint hepatotoxicity corticosteroids"}
            ],
        ),
        question="Research immune checkpoint inhibitor hepatotoxicity clinical management",
    )
    trace = research_plan_trace_payload(plan)

    assert plan.query_candidates == ()
    assert plan.source_preferences == ()
    assert plan.reason_codes == ("proof_ok_no_required_followup",)
    assert plan.warnings == ()
    assert trace["query_count"] == 0
    assert trace["source_preferences"] == []
    assert trace["warnings"] == []


def test_paper_and_preprint_question_prefers_arxiv() -> None:
    plan = build_research_plan(
        _review(followup_questions=(), query_rewrite_candidates=(), coverage_gaps=()),
        question="Find arXiv preprint evidence for transformer diffusion model evaluation",
    )

    assert plan.source_preferences[0].connector_id == "arxiv"
    assert "paper_preprint_source" in plan.source_preferences[0].reason_codes
    assert any("arxiv" in item.query_preview.casefold() for item in plan.query_candidates)


def test_rag_nlp_benchmark_question_prefers_arxiv_without_explicit_preprint_word() -> None:
    plan = build_research_plan(
        _review(followup_questions=(), query_rewrite_candidates=(), coverage_gaps=()),
        question="Compare RAG evaluation benchmark methods for NLP retrieval systems",
    )

    assert plan.source_preferences[0].connector_id == "arxiv"
    assert "paper_preprint_source" in plan.source_preferences[0].reason_codes


def test_local_table_and_json_questions_prefer_local_connectors() -> None:
    table_plan = build_research_plan(
        _review(followup_questions=(), query_rewrite_candidates=(), coverage_gaps=()),
        question="Use the local CSV table dataset to compare rows",
    )
    json_plan = build_research_plan(
        _review(followup_questions=(), query_rewrite_candidates=(), coverage_gaps=()),
        question="Use the local JSON file data",
    )

    assert [item.connector_id for item in table_plan.source_preferences[:3]] == [
        "local_file",
        "csv_tsv",
    ]
    assert [item.connector_id for item in json_plan.source_preferences[:2]] == [
        "local_file",
        "json_file",
    ]


def test_planner_dry_run_payload_and_trace_do_not_store_raw_secret_prompt_url_or_path() -> None:
    plan = build_research_plan(
        _review(
            followup_questions=[
                {
                    "text": (
                        "Find SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX and "
                        "https://example.com/items?token=SECRET and E:/secret/project"
                    )
                }
            ],
            query_rewrite_candidates=[],
        ),
        question=(
            "Research SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX immune therapy "
            "https://example.com/items?token=SECRET "
            f"{Path('E:/secret/project')}"
        ),
    )
    payload = plan.to_payload()
    trace = research_plan_trace_payload(plan)
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    serialized_trace = json.dumps(trace, ensure_ascii=False)

    assert "SECRET_TOKEN" not in serialized_payload
    assert "https://example.com" not in serialized_payload
    assert "E:/secret/project" not in serialized_payload
    assert "SECRET_TOKEN" not in serialized_trace
    assert "https://example.com" not in serialized_trace
    assert "E:/secret/project" not in serialized_trace
    assert "query_preview" not in serialized_trace
    assert trace["plan_ref"].startswith("research_plan:")
    assert trace["dry_run"] is True


def test_research_plan_trace_payload_filters_secret_like_direct_inputs() -> None:
    trace = research_plan_trace_payload({
        "plan_ref": "research_plan:" + "a" * 16,
        "question_digest": "sha256:" + "b" * 64,
        "proof_ref": "research_proof:" + "c" * 16,
        "source_preferences": [
            {"connector_id": "pubmed"},
            {"connector_id": "SECRET_CLIENT_NAME"},
        ],
        "reason_codes": [
            "answer_status_insufficient_evidence",
            "SECRET_CLIENT_NAME",
            "sk-" + "a" * 24,
        ],
        "warnings": [
            "record_pruned_for_ledger_closure",
            "SECRET_CLIENT_NAME",
            "ghp_" + "b" * 24,
        ],
    })

    serialized = json.dumps(trace, ensure_ascii=False)
    assert trace["source_preferences"] == ["pubmed"]
    assert trace["reason_codes"] == ["answer_status_insufficient_evidence"]
    assert trace["warnings"] == ["record_pruned_for_ledger_closure"]
    assert "SECRET_CLIENT_NAME" not in serialized
    assert "sk-" not in serialized
    assert "ghp_" not in serialized


def test_research_plan_trace_payload_drops_malformed_list_fields() -> None:
    trace = research_plan_trace_payload({
        "plan_ref": "research_plan:" + "a" * 16,
        "reason_codes": "SECRET_REASON",
        "warnings": "SECRET_WARNING",
    })

    serialized = json.dumps(trace, ensure_ascii=False)
    assert trace["reason_codes"] == []
    assert trace["warnings"] == []
    assert "SECRET" not in serialized


def test_planner_bounds_query_count_and_does_not_execute_registry_connectors() -> None:
    registry = SourceConnectorRegistry((
        SourceConnectorSpec(
            id="pubmed",
            kind="biomedical_literature",
            status="fixture",
            search_supported=True,
            fetch_supported=True,
            fixture_supported=True,
            shipped=True,
        ),
    ))
    review = _review(
        followup_questions=[
            {"text": f"Find clinical evidence gap {index}"}
            for index in range(20)
        ],
        query_rewrite_candidates=[],
    )

    plan = build_research_plan(
        review,
        question="clinical disease therapy",
        registry=registry,
        max_queries=2,
        max_sources=3,
    )

    assert len(plan.query_candidates) == 2
    assert plan.max_queries == 2
    assert plan.max_sources == 3
    assert plan.source_preferences[0].connector_id == "pubmed"


def test_unavailable_openalex_and_optional_rss_are_not_source_preferences() -> None:
    plan = build_research_plan(
        _review(followup_questions=(), query_rewrite_candidates=(), coverage_gaps=()),
        question="Use OpenAlex RSS feed metadata for topic discovery",
    )

    ids = [item.connector_id for item in plan.source_preferences]
    assert "openalex" not in ids
    assert "rss" not in ids
    assert "openalex_deferred" in plan.warnings
    assert "rss_optional" in plan.warnings
