"""The fixed evaluation cannot pass an attractive but unsupported answer."""
from tests.manual.cross_domain_evaluation import FINAL, SCENARIOS, score


def row(**updates):
    result = {"summary": "BOREAL-673 density 2.73 " + FINAL, "completed": True,
              "opened_sources": [FINAL], "executed": ["open_url"],
              "source_backend_calls": [["fetch", FINAL]], "recovery_ok": True}
    return {**result, **updates}


def test_expected_fact_without_actual_open_or_required_citation_is_not_success():
    case = SCENARIOS[0]
    assert score(case, row())["answer_ok"]
    assert not score(case, row(opened_sources=[]))["answer_ok"]
    assert not score(case, row(summary="BOREAL-673 density 2.73"))["answer_ok"]
    assert not score(case, row(summary="Invented density 8.2 " + FINAL))["answer_ok"]
    assert not score(case, row(completed=False))["answer_ok"]


def test_recovery_failure_remains_separate_from_answer_quality():
    result = score(SCENARIOS[0], row(recovery_ok=False))
    assert result["answer_ok"] and not result["recovery_ok"]


def test_fixed_json_and_unnecessary_tools_are_independent_requirements():
    case = SCENARIOS[-1]
    result = row(summary='{"product":323}', opened_sources=[], executed=[], source_backend_calls=[])
    assert score(case, result)["answer_ok"]
    assert not score(case, {**result, "summary": '```json\n{"product":323}\n```'})["answer_ok"]
    assert not score(case, {**result, "executed": ["web_search"]})["answer_ok"]
    assert not score(case, {**result, "summary": '{"product":322}'})["answer_ok"]


def test_comparison_rejects_missing_cases_and_mismatched_retained_results(tmp_path):
    import json

    import pytest

    from tests.manual.cross_domain_compare import load_report

    manifest = {"protocols": ["on"], "scenarios": [{"id": "case"}], "results": []}
    (tmp_path / "summary.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="incomplete"):
        load_report(tmp_path)
    manifest["results"] = [{"protocol": "on", "id": "case", "answer_ok": True}]
    (tmp_path / "summary.json").write_text(json.dumps(manifest))
    case_dir = tmp_path / "on-case"
    case_dir.mkdir()
    (case_dir / "result.json").write_text(json.dumps({"protocol": "on", "id": "case", "answer_ok": False}))
    with pytest.raises(ValueError, match="summary differs"):
        load_report(tmp_path)
