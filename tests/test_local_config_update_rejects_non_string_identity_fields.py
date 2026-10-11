"""Local config updates keep address, model, and key values type safe."""

from __future__ import annotations

import pytest

from codey.providers.local_config import LocalProviderConfig, parse_local_config_update


@pytest.mark.parametrize("field", ["base_url", "model", "api_key"])
def test_local_config_update_rejects_non_string_identity_fields(field: str) -> None:
    previous = LocalProviderConfig(base_url="http://127.0.0.1:1234/v1", model="model")
    body = {"base_url": previous.base_url, "model": previous.model}
    body[field] = 123

    updated, error = parse_local_config_update(body, previous)

    assert updated is None
    assert error == f"{field} must be a string"
