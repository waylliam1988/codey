"""Only the user-facing summary may be streamed from done arguments."""
from __future__ import annotations

import json

from codey.providers.display_stream import DisplayStream
from codey.providers.summary_stream import summary_prefix


def test_every_split_preserves_escaped_text_and_never_exposes_metadata() -> None:
    summary = '你好，先比较。\nA "quote" and 😀'
    payload: dict[str, object] = {"summary": summary, "collaboration_updates": [{"quote": "PRIVATE"}]}
    arguments = json.dumps(payload)
    observed: list[str] = []
    for index in range(len(arguments) + 1):
        value = summary_prefix(arguments[:index])
        assert summary.startswith(value)
        observed.append(value)
    assert observed[-1] == summary
    assert any(value and value != summary for value in observed)
    for arguments in ('{"metadata":{"summary":"PRIVATE"},"summary":"visible"}',
                      '{"metadata":["PRIVATE"],"summary":"visible"}'):
        assert summary_prefix(arguments) == "visible"


def test_chat_and_responses_emit_summary_before_metadata_finishes() -> None:
    for protocol in ("chat", "responses"):
        seen: list[dict[str, object]] = []
        display = DisplayStream(seen.append)
        if protocol == "responses":
            display.observe({"type": "response.output_item.added", "output_index": 0,
                "item": {"type": "function_call", "name": "done", "arguments": ""}})
        for part in ('{"summary":"First sentence.', ' Second sentence.","collaboration_updates":[{"quote":"PRIVATE"}]}'):
            if protocol == "chat":
                display.observe({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                    **({"name": "done"} if not seen else {}), "arguments": part}}]}}]})
            else:
                display.observe({"type": "response.function_call_arguments.delta", "output_index": 0, "delta": part})
            assert seen and "First sentence." in str(seen[0]["text"])
            assert all("PRIVATE" not in str(event["text"]) for event in seen)
        display.finish("")
        assert seen[-1]["state"] == "complete"
        assert seen[-1]["text"] == "First sentence. Second sentence."


def test_non_done_arguments_and_interrupted_metadata_do_not_leak() -> None:
    seen: list[dict[str, object]] = []
    display = DisplayStream(seen.append)
    display.observe({"choices": [{"delta": {"tool_calls": [{"index": 0,
        "function": {"name": "edit", "arguments": '{"summary":"PRIVATE"}'}}]}}]})
    assert not seen
    assert not summary_prefix('{"metadata":"contains \\"summary\\": \\"PRIVATE\\""}')
