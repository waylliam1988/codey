from codey.providers.snapshot import provider_snapshot
from codey.runtime.core.api_selection import ApiRunSelection


def test_provider_snapshot_freezes_request_interpretation() -> None:
    selection = ApiRunSelection(
        connection_id="local",
        connection_revision="rev-7",
        model_id="model-1",
        protocol="openai-responses",
        native_tools=True,
        context_window_tokens=64000,
        context_reserve_tokens=8000,
        context_keep_recent_tokens=12000,
        thinking_enabled=True,
        reasoning_effort="high",
        stream=True,
        tool_choice="required",
        output_tokens=4000,
        token_counter="provider-tokenizer-v2",
        budget_source="catalog",
        input_limit_tokens=50000,
    )

    snapshot = provider_snapshot("local", selection)

    assert snapshot["provider_id"] == "local"
    assert snapshot["model_id"] == "model-1"
    assert snapshot["protocol"] == "openai-responses"
    assert snapshot["tokenizer_id"] == "provider-tokenizer-v2"
    assert snapshot["provider_session_id"] == ""
    assert snapshot["generation_parameters"] == {
        "thinking_enabled": True,
        "reasoning_effort": "high",
        "stream": True,
        "tool_choice": "required",
        "output_tokens": 4000,
    }
    assert snapshot["capabilities"] == {
        "supports_native_session": False,
        "accepts_history_replay": True,
        "supports_tool_calls": True,
        "supports_images": None,
        "supports_reasoning": True,
        "max_context_tokens": 64000,
    }


def test_non_api_snapshot_is_explicitly_conservative() -> None:
    snapshot = provider_snapshot("web", None)

    assert snapshot["provider_id"] == "web"
    assert snapshot["model_id"] is None
    assert snapshot["protocol"] == "provider-native"
    assert snapshot["tokenizer_id"] is None
    assert snapshot["capabilities"]["supports_tool_calls"] is False


def test_capture_provider_selection_only_resolves_registered_api_providers(monkeypatch) -> None:
    from codey.providers import api_connections
    from codey.providers.snapshot import capture_provider_selection

    selection = object()
    calls = []
    monkeypatch.setattr(api_connections, "capture_selection", lambda provider_id, value: calls.append((provider_id, value)) or selection)

    assert capture_provider_selection("local", None) is selection
    assert calls == [("local", None)]
    assert capture_provider_selection("web", None) is None
    assert calls == [("local", None)]
