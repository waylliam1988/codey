"""The live experiment must distinguish artifacts, workflow and treatment delivery."""
import pytest

from tests.manual.coding_pipeline_value_ab import grade, write_files
from tests.manual.cross_model_quality_ab import artifact_passed, repair_budget, schedule, summarize
from tests.manual.cross_model_quality_cases import FIXTURES


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.case.name)
def test_oracles_accept_reference_and_reject_contract_regressions(tmp_path, fixture):
    write_files(tmp_path, fixture.case.files)
    write_files(tmp_path, fixture.reference)
    quality = grade(tmp_path, fixture.case)
    assert artifact_passed(quality), quality
    for mutant in fixture.mutants:
        write_files(tmp_path, fixture.reference)
        write_files(tmp_path, mutant)
        flawed = grade(tmp_path, fixture.case)
        assert not artifact_passed(flawed)
        assert not flawed["product_behavior_passed"]


def test_schedule_balances_order_and_keeps_stage_applicability():
    cases = [fixture.case for fixture in FIXTURES]
    arms = ("baseline", "plan", "audit", "consensus_one", "consensus_two")
    rows = schedule(cases, arms, 2, 61106)
    assert len(rows) == 70
    assert len(set(rows)) == len(rows)
    assert rows == schedule(cases, arms, 2, 61106)
    for case in cases:
        first = [arm for repeat, name, arm in rows if repeat == 0 and name == case.name]
        second = [arm for repeat, name, arm in rows if repeat == 1 and name == case.name]
        assert second == list(reversed(first))
        assert ("audit" in first) == bool(case.files)
        assert ("consensus_two" in first) == (not case.files)


def test_quality_is_independent_of_stop_reason_and_review_approval():
    quality = {"quality_passed": True, "scope_ok": True}
    assert artifact_passed(quality)
    summary = summarize([{
        "arm": "plan", "artifact_quality_passed": artifact_passed(quality),
        "final_stop_reason": "no_progress", "review_approved": False,
        "treatment_delivered": False,
    }])["plan"]
    assert summary["artifact_quality_passed"] == 1
    assert summary["writer_complete"] == summary["review_approved"] == summary["treatment_delivered"] == 0
    assert not artifact_passed({"quality_passed": True, "scope_ok": False})


def test_repair_uses_remaining_total_budget_and_production_cap():
    assert repair_budget(32, 10) == 12
    assert repair_budget(32, 30) == 2
    assert repair_budget(32, 32) == repair_budget(32, 33) == 0
