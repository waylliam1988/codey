"""Local model text frames must not become native calls on other connections."""
from __future__ import annotations

from codey.providers.api_provider import ApiProvider

FRAME = '<|tool_call>call:tool:done{summary:"finished"}<tool_call|>'


def test_shared_api_preserves_local_looking_text_without_connection_decoder(monkeypatch):
    provider = ApiProvider("http://fixture.test/v1", "online-model")
    assert provider.normalize_reply(FRAME) == FRAME
    monkeypatch.setattr(provider, "_post_chat", lambda *a, **kw: {
        "choices": [{"finish_reason": "stop", "message": {"content": FRAME}}],
    })
    turn = provider.send_turn("Explain this model template", [])
    assert turn.text == FRAME
    assert not turn.tool_calls
    assert "tool_calls" not in provider._messages[-1]


def test_local_connection_installs_its_text_decoder_for_admitted_selection(monkeypatch):
    from codey.providers import local_connection
    from codey.providers.local_config import LocalProviderConfig
    from codey.runtime.core.api_selection import ApiRunSelection

    config = LocalProviderConfig(base_url="http://fixture.test/v1", model="gemma")
    monkeypatch.setattr(local_connection, "config_for_selection", lambda _: config)
    provider = local_connection.open_selection(ApiRunSelection(
        "local", "fixture", "gemma", "openai-completions", True, 32000, 4000, 8000,
    ))
    turn = provider.normalize_reply(FRAME)
    assert turn.tool_calls[0].name == "done"
    monkeypatch.setattr(provider, "_post_chat", lambda *a, **kw: {
        "choices": [{"finish_reason": "stop", "message": {"content": FRAME}}],
    })
    assert provider.send_turn("finish", []).tool_calls[0].name == "done"
