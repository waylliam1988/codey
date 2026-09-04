from __future__ import annotations

from codey.research.followup_quality import followup_usefulness, score_followup_quality_row
from codey.research.source_finalizer_scoring import (
    aggregate_source_finalizer_rows,
    paired_source_finalizer_deltas,
    score_source_finalizer_row,
)
from tests.manual import research_followup_quality_ab
from tests.manual.ab_journal import TRANSCRIPT_MODE_ARCHIVE, TRANSCRIPT_MODE_DIGEST_ONLY


def test_followup_quality_ab_transcript_mode_defaults_to_archive() -> None:
    assert research_followup_quality_ab._transcript_cache_mode("archive") == TRANSCRIPT_MODE_ARCHIVE
    assert research_followup_quality_ab._transcript_cache_mode("digest-only") == TRANSCRIPT_MODE_DIGEST_ONLY
    assert research_followup_quality_ab._transcript_cache_mode("off") is None


def test_followup_quality_score_and_usefulness_are_shared() -> None:
    baseline = {
        "ok": True,
        "score": 3,
        "proof_answer_status": "partial",
        "proof_coverage": 0.62,
        "unsupported_claim_rate": 0.14,
        "record_source_count": 1,
        "record_evidence_count": 1,
        "evidence_count": 1,
        "fixture_fetches": ["https://source-a.test/doc"],
        "provider_send_count": 4,
        "seconds": 12.0,
    }
    planner = {
        "ok": True,
        "proof_ok": True,
        "proof_answer_status": "answered",
        "proof_coverage": 0.91,
        "unsupported_claim_rate": 0.08,
        "record_source_count": 2,
        "record_evidence_count": 3,
        "evidence_count": 3,
        "expected_terms_present": True,
        "fixture_fetches": ["https://source-a.test/doc", "https://source-b.test/doc"],
        "provider_send_count": 6,
        "seconds": 20.0,
        "followup_rounds": 1,
    }
    planner["score"] = score_followup_quality_row(planner)

    usefulness = followup_usefulness(baseline, planner)

    assert planner["score"] == 13
    assert usefulness["useful"] is True
    assert usefulness["material_gain"] is True
    assert usefulness["quality_gain"] is True
    assert usefulness["quality_regression"] is False
    assert usefulness["new_fetched_source_urls"] == ["https://source-b.test/doc"]


def test_followup_quality_treats_nonfinite_numbers_as_unknown() -> None:
    baseline = {"ok": True, "score": float("inf"), "proof_coverage": float("nan")}
    planner = {
        "ok": True,
        "score": 1,
        "proof_coverage": float("inf"),
        "unsupported_claim_rate": float("nan"),
        "followup_rounds": 1,
    }

    usefulness = followup_usefulness(baseline, planner)

    assert usefulness["evaluated"] is True
    assert usefulness["answer_coverage_delta"] == 0.0
    assert usefulness["unsupported_claim_rate_delta"] == 0.0
    assert usefulness["score_delta"] == 1


def test_source_finalizer_scorer_projects_done_stage_rows() -> None:
    baseline = {
        "arm": "baseline",
        "sample": 1,
        "score": 4,
        "done_attempts": 2,
        "quality_retry_count": 1,
        "first_done_passed": False,
        "eventual_done_passed": True,
        "clean_success": False,
        "proof_ok": True,
        "connector_valid": True,
    }
    boundary = {
        "arm": "boundary",
        "sample": 1,
        "stop_reason": "done",
        "opened_target_host": True,
        "evidence_count": 1,
        "proof_ok": True,
        "expected_terms_present": True,
        "done_attempts": 1,
        "quality_retry_count": 0,
        "first_done_passed": True,
        "eventual_done_passed": True,
        "clean_success": True,
        "connector_valid": True,
    }
    boundary["score"] = score_source_finalizer_row(boundary)

    aggregate = aggregate_source_finalizer_rows([baseline, boundary])
    paired = paired_source_finalizer_deltas([baseline, boundary])

    assert boundary["score"] == 11
    assert aggregate["count"] == 2
    assert aggregate["first_pass_rate"] == 0.5
    assert aggregate["average_done_attempts"] == 1.5
    assert paired["boundary"]["paired_samples"] == 1
    assert paired["boundary"]["score_delta_avg"] == 7.0
    assert paired["boundary"]["first_pass_delta"] == 1.0
