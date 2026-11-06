import pytest

from tests.manual.cross_model_quality_summary import compare


def row(case, arm, *, passed, done=True, delivered=True, repeat=0, applicable=True):
    return {"case": case, "arm": arm, "repeat": repeat, "final_stop_reason": "done" if done else "no_progress",
            "treatment_delivered": delivered, "production_stage_applicable": applicable,
            "final": {"quality_passed": passed, "scope_ok": True, "quality_score": 4 if passed else 3}}


def test_paired_quality_keeps_failed_treatments_and_ignores_workflow_labels():
    rows = [row("a", "baseline", passed=True, done=False), row("a", "audit", passed=True),
            row("b", "baseline", passed=False), row("b", "audit", passed=True, done=False),
            row("c", "baseline", passed=True), row("c", "audit", passed=False, delivered=False),
            row("unpaired", "audit", passed=True)]
    result = compare(rows, "audit")
    assert result["count"] == 3
    assert (result["wins"], result["losses"], result["ties"]) == (1, 1, 1)
    assert result["candidate_passed"] == result["baseline_passed"] == 2
    assert not result["pairs"][-1]["candidate_treatment_delivered"]


def test_second_advisor_compares_same_task_and_repeat_not_global_arm_totals():
    rows = [row("a", "consensus_one", passed=False), row("a", "consensus_two", passed=True),
            row("a", "consensus_one", passed=True, repeat=1),
            row("a", "consensus_two", passed=False, repeat=1)]
    result = compare(rows, "consensus_two", "consensus_one")
    assert (result["wins"], result["losses"], result["ties"]) == (1, 1, 0)
    with pytest.raises(ValueError, match="duplicate"):
        compare([*rows, rows[0]], "consensus_two")


def test_existing_plans_are_not_counted_as_production_greenfield_evidence():
    rows = [row("new", "baseline", passed=False), row("new", "plan", passed=True),
            row("existing", "baseline", passed=True), row("existing", "plan", passed=False, applicable=False)]
    assert compare(rows, "plan", production_plan=True)["wins"] == 1
    assert compare(rows, "plan", production_plan=False)["losses"] == 1
