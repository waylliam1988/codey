"""Model settings keep display, connection, and per-chat execution separate."""
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from codey.providers.local_config import LocalProviderConfig


def test_display_name_roundtrips_and_resets_for_another_target():
    from codey.providers.local_config import config_from_dict, config_to_payload, parse_local_config_update

    previous = LocalProviderConfig(base_url="http://localhost:5001/v1", model="full-id", display_name="Gemma4 12B")
    assert config_from_dict(config_to_payload(previous)).display_name == "Gemma4 12B"
    same, error = parse_local_config_update({"base_url": previous.base_url, "model": "full-id"}, previous)
    assert not error and same.display_name == previous.display_name
    other, error = parse_local_config_update({"base_url": "http://localhost:9/v1", "model": "other"}, previous)
    assert not error and other.display_name == ""


def test_chat_selection_is_a_frozen_snapshot_and_never_changes_saved_connection():
    from codey.providers.local_selection import capture_local_run_config

    saved = LocalProviderConfig(base_url="http://localhost:5001/v1", model="default", api_key="private")
    with mock.patch("codey.providers.local_selection.load_local_config", return_value=saved):
        snapshot = capture_local_run_config({"base_url": saved.base_url, "model": "chosen", "thinking": False})
    assert snapshot.model == "chosen" and snapshot.thinking_enabled is False
    assert snapshot.api_key == "private"
    assert saved.model == "default" and saved.thinking_enabled is None
    with pytest.raises(FrozenInstanceError):
        snapshot.model = "late edit"


def test_stale_chat_cannot_move_credentials_to_another_endpoint():
    from codey.providers.local_selection import capture_local_run_config

    saved = LocalProviderConfig(base_url="http://localhost:5001/v1", model="default", api_key="private")
    with mock.patch("codey.providers.local_selection.load_local_config", return_value=saved):
        with pytest.raises(ValueError, match="connection changed"):
            capture_local_run_config({"base_url": "http://other/v1", "model": "chosen"})
        with pytest.raises(ValueError, match="thinking"):
            capture_local_run_config({"base_url": saved.base_url, "model": "chosen", "thinking": "high"})


def test_thinking_capability_requires_active_jinja_and_matching_model():
    from codey.providers.local_selection import thinking_options_from_metadata

    props = {"model_path": "koboldcpp/gemma", "chat_template": "{% if enable_thinking %}think{% endif %}"}
    assert thinking_options_from_metadata(props, {"result": "KoboldCpp", "jinja": True}, "koboldcpp/gemma") == ["off", "high"]
    assert thinking_options_from_metadata(props, {"result": "KoboldCpp", "jinja": False}, "koboldcpp/gemma") == []
    assert thinking_options_from_metadata(props, {"result": "KoboldCpp", "jinja": True}, "other") == []
    assert thinking_options_from_metadata({"chat_template": "thinking"}, {}, "gemma") == []


def test_session_selection_survives_backend_state_roundtrip(tmp_path):
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    selection = {"base_url": "http://localhost:5001/v1", "model": "chosen", "thinking": False}
    store.save({"active_id": "a", "sessions": [{"id": "a", "localSelection": selection}], "projects": []}, base_revision=0)
    assert store.load()["sessions"][0]["localSelection"] == {"base_url":selection["base_url"],"model":"chosen","effort":"off","efforts":{}}


def test_runtime_connect_uses_snapshot_and_rejects_unknown_thinking_control():
    from codey.providers.local_discovery import LocalEndpoint
    from codey.providers.local_openai import LocalOpenAIProvider

    config = LocalProviderConfig(base_url="http://localhost:5001/v1", model="chosen", thinking_enabled=False)
    with mock.patch("codey.providers.local_discovery.resolve_local_endpoint", return_value=LocalEndpoint(config.base_url, ("chosen",))), mock.patch("codey.providers.local_selection.model_metadata", return_value={"thinking_options": ["off", "high"]}):
        provider = LocalOpenAIProvider.connect(config=config, verify_thinking=True)
    assert provider.model == "chosen" and provider.thinking_enabled is False
    with mock.patch("codey.providers.local_discovery.resolve_local_endpoint", return_value=LocalEndpoint(config.base_url, ("chosen",))), mock.patch("codey.providers.local_selection.model_metadata", return_value={"thinking_options": []}), pytest.raises(ValueError, match="thinking"):
        LocalOpenAIProvider.connect(config=config, verify_thinking=True)


def test_autodiscovered_model_can_be_selected_without_saving_a_new_connection():
    from codey.providers.local_discovery import LocalEndpoint
    from codey.providers.local_selection import capture_local_run_config

    with mock.patch("codey.providers.local_selection.load_local_config", return_value=LocalProviderConfig()), mock.patch("codey.providers.local_discovery.resolve_local_endpoint", return_value=LocalEndpoint("http://localhost:5001/v1", ("m",))):
        snapshot = capture_local_run_config({"base_url": "http://localhost:5001/v1", "model": "m"})
    assert snapshot.base_url == "http://localhost:5001/v1" and snapshot.api_key == ""


def test_approval_keeps_admitted_model_and_never_exposes_key_in_status(tmp_path):
    from codey.app.context import AppContext

    state = AppContext(tmp_path)
    try:
        run = state.reserve_run(session_id="a", project=None, task="read", provider_id="local")
        config = LocalProviderConfig(base_url="http://localhost/v1", model="chosen", api_key="secret", thinking_enabled=False)
        state.run_registry.bind_local_config(run.run_id, config)
        state.add_pending_shell_approval("approval", {"run_id":run.run_id, "provider":"local", "session_id":"a"})
        assert "secret" not in str(state.run_state_payload())
        state.release_run(run.run_id)
        assert state.run_registry.local_config_for(run.run_id) is None
        assert state.pop_pending_shell_approval("approval")["_local_config"] is config
    finally:
        state.close()


@pytest.mark.parametrize(('effort', 'enabled', 'wire'), [
    ('off', False, 'none'), ('minimal', True, 'minimal'), ('low', True, 'low'),
    ('medium', True, 'medium'), ('high', True, 'high'),
])
def test_effort_selection_is_captured_and_sent_on_the_wire(effort, enabled, wire):
    from codey.providers.local_discovery import LocalEndpoint
    from codey.providers.local_openai import LocalOpenAIProvider
    from codey.providers.local_selection import capture_local_run_config

    saved = LocalProviderConfig(base_url='http://localhost:5001/v1', model='gemma')
    with mock.patch('codey.providers.local_selection.load_local_config', return_value=saved):
        snapshot = capture_local_run_config({'base_url':saved.base_url, 'model':'gemma', 'effort':effort})
    with mock.patch('codey.providers.local_discovery.resolve_local_endpoint', return_value=LocalEndpoint(saved.base_url, ('gemma',))), mock.patch('codey.providers.local_selection.model_metadata', return_value={'thinking_options':['off','minimal','low','medium','high']}):
        provider = LocalOpenAIProvider.connect(config=snapshot, verify_thinking=True)
    payload = provider._request_payload([{'role':'user','content':'hello'}], None)
    assert payload['chat_template_kwargs'] == {'enable_thinking':enabled}
    assert payload['reasoning_effort'] == wire
    assert saved.thinking_enabled is None


def test_kobold_budget_options_are_version_specific_and_unknown_effort_rejected():
    from codey.providers.local_selection import capture_local_run_config, thinking_options_from_metadata

    props = {'model_path':'gemma','chat_template':'{% if enable_thinking %}thought{% endif %}'}
    version = {'result':'KoboldCpp','jinja':True,'version':'1.117.1'}
    assert thinking_options_from_metadata(props, version, 'gemma') == ['off','minimal','low','medium','high']
    assert thinking_options_from_metadata(props, {**version,'version':'unknown'}, 'gemma') == ['off','high']
    with mock.patch('codey.providers.local_selection.load_local_config', return_value=LocalProviderConfig(base_url='http://localhost/v1')), pytest.raises(ValueError, match='effort'):
        capture_local_run_config({'base_url':'http://localhost/v1','model':'m','effort':'fictional'})


def test_runtime_rejects_an_effort_not_in_current_model_capabilities():
    from codey.providers.local_discovery import LocalEndpoint
    from codey.providers.local_openai import LocalOpenAIProvider

    config = LocalProviderConfig(base_url='http://localhost/v1', model='m', thinking_enabled=True, reasoning_effort='max')
    with mock.patch('codey.providers.local_discovery.resolve_local_endpoint', return_value=LocalEndpoint(config.base_url, ('m',))), mock.patch('codey.providers.local_selection.model_metadata', return_value={'thinking_options':['off','high']}), pytest.raises(ValueError, match='effort'):
        LocalOpenAIProvider.connect(config=config, verify_thinking=True)


def test_effort_history_survives_reload_and_malformed_values_are_bounded(tmp_path):
    from codey.storage.ui_state_store import UiStateStore

    store = UiStateStore(tmp_path)
    selection = {'base_url':'http://localhost/v1','model':'m','effort':'low','efforts':{'m':'low','bad':[]}}
    store.save({'sessions':[{'id':'a','localSelection':selection}],'active_id':'a','projects':[]}, base_revision=0)
    assert store.load()['sessions'][0]['localSelection'] == {**selection,'efforts':{'m':'low'}}


@pytest.mark.parametrize('invalid', [[], {}, 1, 'default', 'on'])
def test_invalid_effort_is_a_validation_error(invalid):
    from codey.providers.local_selection import capture_local_run_config

    with mock.patch('codey.providers.local_selection.load_local_config', return_value=LocalProviderConfig(base_url='http://localhost/v1')), pytest.raises(ValueError, match='effort'):
        capture_local_run_config({'base_url':'http://localhost/v1','model':'m','effort':invalid})
