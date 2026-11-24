import pytest

from codey.completion.repair_context import DecisiveCheckFact, project_repair_context


@pytest.mark.parametrize("budget", [40, 400])
def test_repair_prompt_fits_budget_and_retains_an_observed_check(budget: int) -> None:
    proof = {"status": "failed", "checks": [{"check_id": "verification", "status": "fail"}]}
    check = DecisiveCheckFact(
        command="pytest tests/test_calculator.py",
        exit_code=1,
        result_summary="Assertion failed in module calculation. " * 20,
    )

    result = project_repair_context(
        proof=proof,
        failure_class="product_failure",
        decisive_checks=(check,),
        budget_chars=budget,
    )

    assert result.truncated
    assert len(result.prompt_text) <= budget
    if budget == 40:
        assert not result.admitted
        assert result.prompt_text == ""
    else:
        assert result.admitted
        assert "Observed failing checks:" in result.prompt_text
        assert check.command in result.prompt_text
        assert "Exit: 1" in result.prompt_text
