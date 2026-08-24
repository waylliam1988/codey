from __future__ import annotations


from tests.manual import longitudinal_research_harness_ab as harness


def test_longitudinal_harness_self_test() -> None:
    harness._self_test()


def test_deterministic_run_passes_all_development_cases() -> None:
    assert harness.run_deterministic(list(harness.development_case_ids())) == 0


def test_stale_rounds_flag_only_after_refresh() -> None:
    outcomes, reasons = harness.scenario_stale_claim_refresh()
    assert reasons == []
    baseline, refreshed = outcomes
    assert not baseline.report.observable("stale_source_flagged")
    assert refreshed.report.observable("stale_source_flagged")
    assert refreshed.report.observable("answered")
    # The revised conclusion is the only claim left; the old ref stays stable.
    assert refreshed.report.metrics["claim_count"] == 1
    assert refreshed.brief_render.count("stable-v3") >= 1


def test_conflicting_evidence_creates_finding_and_gap() -> None:
    (outcome,), reasons = harness.scenario_conflicting_evidence_gap()
    assert reasons == []
    assert outcome.report.observable("conflicting_evidence_finding")
    assert outcome.report.observable("counterevidence_checked")
    assert outcome.report.observable("planner_gap_created")
    # The unsupported strong claim is visible, but never a constraint.
    assert outcome.report.observable("unsupported_claim_present")


def test_injected_unsupported_claim_never_reaches_constraints() -> None:
    outcomes, reasons = harness.scenario_unsupported_claim_injection()
    assert reasons == []
    final = outcomes[1].report
    assert final.observable("unsupported_claim_present")
    assert not final.observable("unsupported_in_constraints")
    assert "[unsupported]" in outcomes[1].brief_render


def test_failed_analysis_is_not_overclaimed() -> None:
    _, failed_reasons = harness.scenario_local_csv_pdf_analysis(command_failed=True)
    assert failed_reasons == []
