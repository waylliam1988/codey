from __future__ import annotations

import json

from tests.manual import ab_harness_common as common
from tests.manual import research_claim_support_projection as projection


def test_claim_support_projection_flags_record_claims_without_copying_raw_text() -> None:
    row = {
        "provider": "mimo",
        "case": "pubmed",
        "arm": "connector",
        "summary_text": "RAW REPORT SHOULD NOT BE COPIED",
    }
    common.attach_research_record_payload(row, projection._self_test_record())
    payload = projection.build_projection_from_inputs(
        [(
            "record-result.json",
            {
                "probe": "source_connector_ab",
                "rows": [row],
            },
        )],
        question="Research helium supply",
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    item = payload["items"][0]

    assert row["research_record_included"] is True
    assert payload["manual_only"] is True
    assert payload["record_projection_count"] == 1
    assert payload["target_gap_counts"] == {
        "claim_missing_citation": 1,
        "claim_missing_evidence_ref": 1,
        "claim_missing_support_relation": 1,
        "claim_not_evidence_backed": 1,
    }
    assert item["projection_kind"] == "record_claim_support_projection"
    assert item["problem_claim_refs"] == ["claim:0000000000000004"]
    assert item["projected_delete"]["improves_target_gaps"] is True
    assert item["projected_delete"]["target_gap_total_after"] == 0
    assert item["projected_downgrade"]["improves_target_gaps"] is True
    assert "Raw unsupported pricing claim" not in serialized
    assert "RAW REPORT SHOULD NOT BE COPIED" not in serialized


def test_claim_support_projection_summarizes_row_only_gaps_without_raw_text() -> None:
    payload = projection.build_projection_from_inputs(
        [(
            "row-result.json",
            {
                "probe": "research_followup_quality_ab",
                "rows": [{
                    "provider": "mimo",
                    "case": "pubmed",
                    "arm": "planner",
                    "proof_ok": False,
                    "proof_answer_status": "partial",
                    "proof_missing_evidence": [
                        "claim_missing_citation",
                        "claim_missing_support_relation",
                    ],
                    "summary_preview": (
                        "## 结论\n"
                        "- RAW CLAIM WITHOUT CITATION\n\n"
                        "## 关键证据\n"
                        "- RAW EVIDENCE [1]\n\n"
                        "## 来源\n"
                        "[1] Example - https://example.com"
                    ),
                }],
            },
        )]
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    item = payload["items"][0]

    assert payload["row_gap_summary_count"] == 1
    assert item["projection_kind"] == "row_gap_summary"
    assert item["can_project_claims"] is False
    assert item["reason_code"] == "missing_research_record"
    assert item["target_gap_counts"]["claim_missing_citation"] == 1
    assert item["target_gap_counts"]["claim_missing_support_relation"] == 1
    assert item["report_probe"]["claim_missing_citation_estimate"] == 1
    assert "RAW CLAIM WITHOUT CITATION" not in serialized
    assert "RAW EVIDENCE" not in serialized


def test_claim_support_projection_report_probe_uses_digest_only() -> None:
    payload = projection.build_projection_from_inputs(
        [("archived-transcript.json", {"prompt": "RAW PROMPT", "reply": "## 结论\n- RAW REPLY CLAIM"})]
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    item = payload["items"][0]

    assert payload["report_probe_count"] == 1
    assert item["projection_kind"] == "report_digest_probe"
    assert item["report_probe"]["claim_missing_citation_estimate"] == 1
    assert "RAW PROMPT" not in serialized
    assert "RAW REPLY CLAIM" not in serialized


def test_claim_support_projection_self_test() -> None:
    projection._self_test()
