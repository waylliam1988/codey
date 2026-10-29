"""Constrained compatibility output uses the existing task/provider lifecycle."""

from dataclasses import replace

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers import api_transport, local_config, local_connection, local_selection
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.api_selection import ApiRunSelection


def test_json_mode_roundtrips_and_rejects_incompatible_settings():
    previous = local_config.LocalProviderConfig()
    body = {"base_url": "http://fixture/v1", "model": "fixture", "native_tools_mode": "json"}
    config, error = local_config.parse_local_config_update(body, previous)
    assert not error and config.thinking_enabled is False
    assert local_config.config_from_dict(local_config.config_to_payload(config)) == config
    for overrides in ({"thinking_enabled": True}, {"api_protocol": "openai-responses"}):
        assert local_config.parse_local_config_update({**body, **overrides}, previous)[0] is None


def test_frozen_json_selection_survives_changed_connection_defaults(monkeypatch):
    config = local_config.LocalProviderConfig(base_url="http://fixture/v1", model="fixture",
        native_tools_mode="json", thinking_enabled=False, connection_revision="fixture")
    selection = ApiRunSelection("local", local_connection._target_revision(config), "fixture", "openai-completions", False,
                                thinking_enabled=False, text_tool_format="json_schema")
    restored = ApiRunSelection.from_payload(selection.to_payload())
    monkeypatch.setattr(local_config, "load_local_config", lambda: replace(config, native_tools_mode="on", thinking_enabled=True))
    monkeypatch.setattr(local_selection, "load_local_config", lambda: config)
    assert local_selection.capture_local_run_config({"base_url": config.base_url, "model": config.model}).thinking_enabled is False
    provider = local_connection.open_selection(restored)
    try:
        assert provider.native_tools is False and provider.thinking_enabled is False
        assert provider.text_tool_format == "json_schema"
    finally:
        provider.close()
    with pytest.raises(ValueError, match="thinking disabled"):
        replace(selection, thinking_enabled=True)


def test_ordinary_and_auxiliary_requests_are_not_forced_into_tool_envelopes(monkeypatch):
    payloads = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        text = '{"tool":"done","args":{"summary":"two"}}' if "response_format" in payload else "ordinary prose"
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    auxiliary = provider.fork_auxiliary()
    try:
        assert provider.send("hello") == "ordinary prose"
        assert '"done"' in provider.send("task", tool_call_json=True)
        assert auxiliary.send("summarize") == "ordinary prose"
        assert ["response_format" in p for p in payloads] == [False, True, False]
        schema = payloads[1]["response_format"]["json_schema"]["schema"]
        assert schema["properties"] == {"tool": {"type": "string"}, "args": {"type": "object"}}
    finally:
        auxiliary.close()
        provider.close()


@pytest.mark.usefixtures("no_external_advisor_models")
def test_headless_task_admits_constrained_request_through_shared_kernel(tmp_path, monkeypatch):
    payloads = []

    def generate(_url, payload, _headers, **_kwargs):
        payloads.append(payload)
        return {"choices": [{"message": {"content": '{"tool":"done","args":{"summary":"2"}}'}, "finish_reason": "stop"}]}

    monkeypatch.setattr(api_transport, "generate", generate)
    provider = ApiProvider("http://fixture/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    result = run_headless(HeadlessRequest(project=None, task="What is 1+1?", intent="hybrid", provider_id="qwen",
        session_id="json-task", state_home=tmp_path / "state", research_store_root=tmp_path / "knowledge"),
        connect_provider=lambda *_args, **_kwargs: provider, emit_jsonl=lambda _event: None)
    assert result.exit_code == 0 and result.stop_reason == "done"
    assert payloads and all("response_format" in p for p in payloads)
