"""Contracts for the sentence-level query-guidance experiment."""
from __future__ import annotations

from tools.browser_policy_options import GUIDANCE, QUERY_GUIDANCE_ABLATIONS
from tools.query_guidance_ablation import CASES, VARIANTS


def test_query_ablation_contains_full_and_leave_one_out_variants() -> None:
    assert VARIANTS == ("", "query", "query_only_evidence", "query_no_subject", "query_no_terms",
                        "query_no_evidence")
    assert set(QUERY_GUIDANCE_ABLATIONS) == {
        "query_only_evidence", "query_no_subject", "query_no_terms", "query_no_evidence",
    }
    full = GUIDANCE["query"]
    assert full.startswith("Form each search around the exact subject")
    assert full.endswith("obtain the original passage before citing a factual answer.")
    assert "Form each search around" not in QUERY_GUIDANCE_ABLATIONS["query_no_subject"]
    assert "original terminology" not in QUERY_GUIDANCE_ABLATIONS["query_no_terms"]
    assert "obtain the original passage" not in QUERY_GUIDANCE_ABLATIONS["query_no_evidence"]
    assert QUERY_GUIDANCE_ABLATIONS["query_only_evidence"] == (
        "A search hit is only a lead; obtain the original passage before citing a factual answer."
    )


def test_ablation_uses_version_and_missing_exception_cases() -> None:
    assert CASES == ("policy_ranked_search", "policy_followup_search")
