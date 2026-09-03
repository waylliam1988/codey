from __future__ import annotations

import json
from pathlib import Path

from tests.manual import research_experiment_gate as gate
from tests.manual import research_followup_quality_ab as followup_ab


def test_research_experiment_gate_self_test() -> None:
    gate._self_test()


def test_connector_backed_followup_harness_self_test() -> None:
    followup_ab._self_test()


def test_research_experiment_gate_scores_bounded_files_without_raw_bodies(tmp_path: Path) -> None:
    result = tmp_path / "research_followup_quality_ab-mimo.json"
    result.write_text(
        json.dumps(
            {
                "probe": "research_followup_quality_ab",
                "provider": "mimo",
                "rows": [
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "baseline",
                        "ok": True,
                        "score": 5,
                        "record_source_count": 1,
                        "record_evidence_count": 1,
                        "unsupported_claim_rate": 0.3,
                        "provider_send_count": 4,
                        "summary_text": "RAW BASELINE REPORT SHOULD NOT BE COPIED",
                    },
                    {
                        "provider": "mimo",
                        "case": "pubmed",
                        "arm": "planner",
                        "ok": True,
                        "score": 6,
                        "proof_coverage": 0.8,
                        "record_source_count": 2,
                        "record_evidence_count": 2,
                        "unsupported_claim_rate": 0.2,
                        "provider_send_count": 5,
                        "followup_rounds": 1,
                        "ab_followup_mode": "production_evidence_followup",
                        "summary_text": "RAW PLANNER REPORT SHOULD NOT BE COPIED",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    trace = tmp_path / "research_followup_quality_ab-mimo.trace.json"
    trace.write_text('{"probe":"trace","rows":[{"prompt":"RAW PROMPT"}]}', encoding="utf-8")

    payload = gate.build_gate(gate.load_payloads(gate.expand_inputs([str(tmp_path / "*.json")])))
    serialized = json.dumps(payload)

    assert payload["source_file_count"] == 1
    assert payload["bounded_followup"]["pair_count"] == 1
    assert payload["bounded_followup"]["safe_evidence_only_useful_count"] == 1
    assert "RAW BASELINE REPORT" not in serialized
    assert "RAW PLANNER REPORT" not in serialized
    assert "RAW PROMPT" not in serialized


def test_research_experiment_gate_keeps_source_wrapper_manual_without_ab() -> None:
    payload = gate.build_gate([])
    decisions = {item["feature"]: item for item in payload["default_path_decisions"]}

    assert decisions["untrusted source wrapper"]["decision"] == "do_not_promote_without_ab"
    assert decisions["untrusted source wrapper"]["supporting_counts"]["rows"] == 0


def test_research_experiment_gate_skips_incomplete_result_files(tmp_path: Path) -> None:
    complete = tmp_path / "research_followup_quality_ab-mimo-complete.json"
    incomplete = tmp_path / "research_followup_quality_ab-mimo-incomplete.json"
    rows = [
        {
            "provider": "mimo",
            "case": "pubmed",
            "arm": "baseline",
            "ok": True,
            "score": 5,
            "record_source_count": 1,
            "record_evidence_count": 1,
            "unsupported_claim_rate": 0.3,
            "provider_send_count": 4,
        },
        {
            "provider": "mimo",
            "case": "pubmed",
            "arm": "planner",
            "ok": True,
            "score": 6,
            "proof_coverage": 0.8,
            "record_source_count": 2,
            "record_evidence_count": 2,
            "unsupported_claim_rate": 0.2,
            "provider_send_count": 5,
            "followup_rounds": 1,
            "ab_followup_mode": "production_evidence_followup",
        },
    ]
    complete.write_text(
        json.dumps({"probe": "research_followup_quality_ab", "complete": True, "rows": rows}),
        encoding="utf-8",
    )
    incomplete.write_text(
        json.dumps({
            "probe": "research_followup_quality_ab",
            "complete": False,
            "rows": [dict(rows[0], score=0)],
        }),
        encoding="utf-8",
    )

    payload = gate.build_gate(gate.load_payloads(gate.expand_inputs([str(tmp_path / "*.json")])))

    assert payload["source_file_count"] == 1
    assert payload["source_files"] == [complete.name]
    assert payload["skipped_incomplete_files"] == [incomplete.name]
    assert payload["bounded_followup"]["row_count"] == 2
    assert payload["bounded_followup"]["pair_count"] == 1
