"""Reproduce the current public model directory's scoped endpoint contract."""

from codey.providers.zen.catalog import parse_models
from codey.providers.zen.identity import ZEN_BASE_URL, ZenIdentity


def test_current_directory_matches_the_generation_destination():
    endpoint = "https://opencode.ai/inference/openai/v1"
    directory = {"opencode": {"api": endpoint, "npm": "@ai-sdk/openai-compatible", "models": {
        "free": {"provider": {"api": endpoint}, "tool_call": True,
                 "cost": {"input": 0, "output": 0}, "limit": {"context": 200000, "output": 32000}},
    }}}
    assert [item.id for item in parse_models(directory)] == ["free"]
    assert endpoint == ZEN_BASE_URL
    assert ZenIdentity().headers(endpoint)["x-opencode-client"] == "cli"


def test_model_cannot_override_generation_destination():
    directory = {"opencode": {"api": ZEN_BASE_URL, "npm": "@ai-sdk/openai-compatible", "models": {
        "free": {"provider": {"api": "https://unrelated.test/v1"}, "tool_call": True,
                 "cost": {"input": 0, "output": 0}, "limit": {"context": 200000, "output": 32000}},
    }}}
    assert not parse_models(directory)
