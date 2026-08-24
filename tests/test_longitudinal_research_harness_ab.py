from __future__ import annotations

import json

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
    # The round states only the current conclusion; the superseded stable-v2
    # claim is never restated, so the Writer handoff carries exactly one
    # verified constraint and no mutually exclusive pair.
    assert refreshed.report.metrics["claim_count"] == 1
    assert refreshed.brief_render.count("constraint [verified]") == 1
    assert "stable-v3" in refreshed.brief_render
    assert "stable-v2" not in refreshed.brief_render
    # Supersession is still explicit against the retained old evidence.
    assert refreshed.report.observable("conflicting_evidence_finding") is True


def test_deterministic_summary_surfaces_review_ok(capsys) -> None:
    exit_code = harness.run_deterministic(list(harness.development_case_ids()))
    assert exit_code == 0

    printed = capsys.readouterr().out
    payload = json.loads(printed.split("\ndeterministic")[0])
    assert payload["summary"]
    for case_id, block in payload["summary"].items():
        rounds = block["rounds"]
        assert rounds, case_id
        # review_ok is surfaced per round so gate passage ("projection
        # regression passed") is never misread as proof-quality passage.
        assert all("review_ok" in row for row in rounds), case_id


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
