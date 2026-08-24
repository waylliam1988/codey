from __future__ import annotations

from tests.manual import ghost_research_continuity_ab as ab


def test_continuity_arm_admits_bounded_hints_and_baseline_stays_empty() -> None:
    payload = ab.run_cases(provider_id="fake", provider_factory=None)

    assert payload["ok"]
    summary = payload["summary"]
    assert summary["continuity"]["exact"] == summary["continuity"]["total"]
    assert summary["baseline"]["exact"] == summary["baseline"]["total"]
    # Baseline arm keeps the profile gate closed: nothing admitted anywhere.
    assert summary["baseline"]["admitted"] == 0
    assert summary["continuity"]["admitted"] >= 3
    assert summary["attribution"]["prior_claims_flagged"] == 1
    assert summary["continuity"]["internal_leaks"] == 0


def test_old_claim_is_carried_as_a_permanently_stale_ref() -> None:
    case = next(
        item for item in ab.DEFAULT_CASES
        if item.name == "old-claim-must-be-rechecked"
    )

    row = ab._run_case(case, arm="continuity", provider_factory=None)

    assert row["exact"]
    assert row["prior_claim_flagged"]
    assert row["digest_only_payload"]


def test_failure_classification_separates_provider_and_planner_causes() -> None:
    assert ab.classify_outcome(
        sends=2, replies=2, send_error_text="", stop_reason="done"
    ) == "ok"
    assert ab.classify_outcome(
        sends=1, replies=0, send_error_text="TimeoutError: send", stop_reason=""
    ) == "native_search_stall_suspected"
    assert ab.classify_outcome(
        sends=1, replies=0, send_error_text="", stop_reason=""
    ) == "native_search_stall_suspected"
    assert ab.classify_outcome(
        sends=0, replies=0, send_error_text="ConnectionError", stop_reason=""
    ) == "provider_send_error"
    assert ab.classify_outcome(
        sends=4, replies=4, send_error_text="", stop_reason="no_progress"
    ) == "planner_quality:no_progress"
