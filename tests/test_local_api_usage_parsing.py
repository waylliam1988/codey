"""Local usage protocols preserve unknown fields and nullable optional details."""
import pytest

from codey.providers.local_usage import parser_for


@pytest.mark.parametrize("protocol,usage", [
    ("openai-completions", {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None}),
    ("openai-responses", {"input_tokens": 10, "output_tokens": 5, "input_tokens_details": None}),
])
def test_optional_null_details_preserve_reported_input_and_output(protocol, usage):
    result = parser_for(protocol)({"usage": usage})
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.cached_input_tokens is None


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_absent_usage_is_unknown_and_invalid_usage_is_not_coerced(protocol):
    parse = parser_for(protocol)
    assert parse({"usage": None}) is None
    with pytest.raises(ValueError):
        parse({"usage": []})
