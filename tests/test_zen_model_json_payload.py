"""Catalog models must already be JSON values before crossing app boundaries."""

from __future__ import annotations

import pytest

from codey.providers.zen.catalog import ZenModel
from codey.types.json_value import decode_json_object


@pytest.mark.parametrize("efforts", [(), ("low", "high")])
def test_model_payload_encodes_efforts_as_a_json_array(efforts: tuple[str, ...]) -> None:
    model = ZenModel(
        "fixture", "Fixture", "openai-responses", 32768, 4096,
        "https://opencode.ai/inference/openai/v1", efforts, 30000, True,
    )

    payload = model.to_payload()

    assert decode_json_object(payload) == {
        "id": "fixture", "name": "Fixture", "protocol": "openai-responses",
        "context": 32768, "output": 4096, "api": model.api,
        "efforts": list(efforts), "input_limit_tokens": 30000, "thinking_toggle": True,
    }
    assert model.efforts == efforts
