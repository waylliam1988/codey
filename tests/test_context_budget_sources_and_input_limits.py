"""Independent model input ceilings narrow, never replace, the selected budget."""
import pytest

from codey.providers.token_accounting import ContextBudget
from codey.providers.zen.catalog import parse_models
from codey.runtime.core.api_selection import ApiRunSelection


def test_independent_input_limit_narrows_residual_capacity():
    budget = ContextBudget(262144, 8192, 1024, 12000, input_limit_tokens=100000)
    assert budget.input_limit == 100000
    assert ContextBudget(32768, 8192, 0, 12000, input_limit_tokens=100000).input_limit == 24576


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_invalid_independent_input_limit_is_rejected(value):
    with pytest.raises(ValueError):
        ContextBudget(32768, 8192, 0, 12000, input_limit_tokens=value)


def test_selection_round_trip_preserves_independent_input_limit():
    selection = ApiRunSelection("local", "revision", "model", "openai-completions", True,
                                input_limit_tokens=16000)
    assert ApiRunSelection.from_payload(selection.to_payload()).input_limit_tokens == 16000


def test_zen_directory_keeps_unknown_input_unknown_and_parses_advertised_limit():
    model = {"cost": {"input": 0, "output": 0}, "tool_call": True,
             "limit": {"context": 262144, "output": 8192}, "provider": {"npm": "@ai-sdk/openai"}}
    directory = {"opencode": {"models": {"model": model}}}
    assert parse_models(directory, {"model"})[0].input_limit_tokens is None
    model["limit"]["input"] = 100000
    assert parse_models(directory, {"model"})[0].input_limit_tokens == 100000
    model["limit"]["input"] = -1
    assert parse_models(directory, {"model"}) == ()
