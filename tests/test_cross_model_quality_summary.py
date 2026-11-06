import pytest

from tests.manual.cross_model_quality_summary import compare, quality_delta, report


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


def quality(*passed):
    return {"quality": [{"name": str(i), "ok": value} for i, value in enumerate(passed)],
            "quality_passed": all(passed), "quality_score": sum(passed),
            "visible_suite": {"ok": True}, "scope_ok": True}


def test_fixing_some_bugs_is_a_gain_without_requiring_every_bug_to_be_found():
    before, after = quality(True, False, False), quality(True, True, False)
    delta = quality_delta(before, after)
    assert delta["verified_gain_without_regression"]
    assert delta["fixed_behavior_checks"] == ["1"] and not after["quality_passed"]
    assert delta["outcome"] == "benefit"
    mixed = quality_delta(before, quality(False, True, False))
    assert mixed["outcome"] == "mixed" and not mixed["verified_gain_without_regression"]


def test_local_writer_only_and_online_review_compare_the_exact_same_candidate():
    initial, final = quality(True, False, False), quality(True, True, False)
    data = row("a", "baseline", passed=False)
    data["initial"] = {"quality": initial}
    data["final"] = final
    result = compare([data], "baseline", baseline_stage="initial")
    assert result["count"] == result["verified_benefits"] == 1
    assert result["wins"] == result["losses"] == 0
    assert result["checked_regressions"] == result["mixed_changes"] == 0


def test_failed_local_writer_is_not_dropped_from_cooperation_comparison():
    baseline = row("a", "baseline", passed=False, done=False)
    baseline["final"] = quality(False, False)
    candidate = row("a", "plan", passed=True)
    candidate["final"] = quality(True, True)
    result = compare([baseline, candidate], "plan", baseline_stage="initial")
    assert result["count"] == result["verified_benefits"] == 1
    assert not result["pairs"][0]["baseline_initial_available"]


def test_interrupted_repair_is_included_using_actual_final_files():
    data = row('a', 'baseline', passed=False)
    data.update(initial={'quality': quality(True, False, False)}, final=quality(True, True, False),
                artifact_quality_passed=False, failure_stage='repair', error='OutputLengthError')
    result = report({'metadata': {'schedule': [[0, 'a', 'baseline']]}, 'rows': [data]})
    assert result['summary']['baseline']['review_repair_attempts'] == 1
    assert result['summary']['baseline']['review_repairs'] == 0
    assert result['repairs'][0]['repair_execution_failed']
    assert result['repairs'][0]['checked_quality_delta']['verified_gain_without_regression']
