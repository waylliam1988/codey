"""Local HTTP protocol is independent of native tools and frozen at admission."""
from dataclasses import replace

import pytest

from codey.providers import local_config
from codey.providers.api_provider import ApiProvider
from codey.providers.native_tools import supports_native_tools


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_local_configuration_roundtrips_explicit_protocol(protocol):
    previous = local_config.LocalProviderConfig(base_url="http://localhost:9/v1", model="test")
    updated, error = local_config.parse_local_config_update({"base_url": previous.base_url, "model": "test", "api_protocol": protocol}, previous)
    assert not error
    assert updated.api_protocol == protocol
    assert local_config.config_from_dict(local_config.config_to_payload(updated)) == updated


def test_unknown_protocol_is_rejected_without_guessing_chat():
    updated, error = local_config.parse_local_config_update({"base_url": "http://localhost:9/v1", "api_protocol": "anthropic"}, local_config.LocalProviderConfig())
    assert updated is None and "protocol" in error


def test_running_api_native_mode_does_not_read_changed_settings(monkeypatch):
    monkeypatch.delenv("NATIVE_TOOLS", raising=False)
    monkeypatch.setattr(local_config, "load_local_config", lambda: replace(local_config.LocalProviderConfig(), native_tools_mode="off"))
    provider = ApiProvider("http://localhost:9/v1", "test", native_tools=True)
    assert supports_native_tools(provider, "local") is True


def test_settings_save_retains_protocol_after_successful_probe(monkeypatch):
    from codey.app import api
    from codey.providers.local_discovery import LocalEndpoint

    monkeypatch.setattr(api, "load_local_config", lambda: local_config.LocalProviderConfig())
    monkeypatch.setattr(api, "probe_local_endpoint_detail", lambda *args, **kwargs: (LocalEndpoint("http://localhost:9/v1", ("test",)), "ok"))
    saved = []
    monkeypatch.setattr(api, "save_local_config", saved.append)
    monkeypatch.setattr(api, "_attach_local_metadata", lambda payload: None)
    status, _payload = api.save_local_provider_response({"base_url": "http://localhost:9/v1", "model": "test", "api_protocol": "openai-responses"})
    assert status == 200
    assert saved[0].api_protocol == "openai-responses"
