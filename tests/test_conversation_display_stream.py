from codey.providers.api_provider import ApiProvider


def test_streamed_native_text_reaches_display_before_generation_finishes(monkeypatch):
    seen = []
    provider = ApiProvider(base_url="http://127.0.0.1:1/v1", model="test", stream=True, native_tools=True)
    provider.bind_display(seen.append)

    def generate(*args, on_event, **kwargs):
        on_event({"choices": [{"index": 0, "delta": {"content": "第一段。\n\n"}}]})
        assert seen and seen[-1]["text"] == "第一段。\n\n"
        on_event({"choices": [{"index": 0, "delta": {"tool_calls": [{"function": {"arguments": "SECRET TOOL JSON"}}]}}]})
        return {"choices": [{"message": {"content": "第一段。\n\n第二段。"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("codey.providers.api_transport.generate", generate)
    assert provider.send_turn("hello").text == "第一段。\n\n第二段。"
    assert seen[-1]["state"] == "complete"
    assert seen[-1]["text"] == "第一段。\n\n第二段。"
    assert len({event["stream_id"] for event in seen}) == 1
    assert all("SECRET" not in event["text"] for event in seen)
    assert [event["sequence"] for event in seen] == sorted({event["sequence"] for event in seen})


def test_failed_stream_keeps_partial_text_and_text_protocols_are_not_exposed(monkeypatch):
    import pytest

    from codey.providers.api_transport import GenerationUnknownError

    seen = []
    provider = ApiProvider(base_url="http://127.0.0.1:1/v1", model="test", stream=True)
    provider.bind_display(seen.append)

    def generate(*args, on_event, **kwargs):
        on_event({"type": "response.output_text.delta", "delta": "partial"})
        raise GenerationUnknownError("disconnected")

    monkeypatch.setattr("codey.providers.api_transport.generate", generate)
    with pytest.raises(GenerationUnknownError):
        provider.send_turn("hello")
    assert seen[-1]["state"] == "interrupted"
    assert seen[-1]["text"] == "partial"
    seen.clear()
    with pytest.raises(GenerationUnknownError):
        provider.send("text-only protocol prompt")
    assert seen == []
