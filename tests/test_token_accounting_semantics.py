"""Context bounds and reported usage keep unknown values and inclusive totals."""
import pytest

from codey.providers.token_accounting import ContextBudget, ReportedUsage, RequestContextCount


def test_unknown_is_not_zero_and_reasoning_and_cache_are_inclusive_details():
    assert ReportedUsage().input_tokens is None
    usage = ReportedUsage(100, 50, cached_input_tokens=80, reasoning_output_tokens=30)
    assert usage.input_tokens + usage.output_tokens == 150
    assert RequestContextCount(0, "tokenizer").value == 0


@pytest.mark.parametrize("values", [(100, 90, 10, 20), (100, -1, 0, 20), (100, 10, -1, 20), (True, 10, 0, 20)])
def test_invalid_context_budgets_are_rejected(values):
    with pytest.raises(ValueError):
        ContextBudget(*values)


@pytest.mark.parametrize("values", [(None, "tokenizer"), (0, "unknown"), (True, "estimated"), (-1, "tokenizer")])
def test_invalid_context_counts_are_rejected(values):
    with pytest.raises(ValueError):
        RequestContextCount(*values)


@pytest.mark.parametrize("usage", [{"input_tokens": True}, {"output_tokens": -1},
                                  {"input_tokens": 10, "cached_input_tokens": 11},
                                  {"output_tokens": 10, "reasoning_output_tokens": 11}])
def test_invalid_reported_usage_is_rejected(usage):
    with pytest.raises(ValueError):
        ReportedUsage(**usage)
