"""Stream variants must preserve call identities without guessing parallel calls."""
import io
import json
import time

import pytest

from codey.providers.api_transport import GenerationUnknownError, read_sse


def stream_calls(deltas):
    frames = [{"choices": [{"delta": {"tool_calls": calls}, "finish_reason": None}]} for calls in deltas]
    frames.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    data = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames)
    return read_sse(io.BytesIO(data + b"data: [DONE]\n\n"), deadline=time.monotonic() + 2,
                    cancelled=lambda: False)["choices"][0]["message"]["tool_calls"]


def test_indexless_single_call_and_repeated_id_are_preserved():
    calls = stream_calls([
        [{"id": "call-1", "function": {"name": "read", "arguments": '{"path":'}}],
        [{"id": "call-1", "function": {"arguments": '"probe.txt"'}}],
        [{"function": {"arguments": "}"}}],
    ])
    assert calls == [{"id": "call-1", "type": "function",
                      "function": {"name": "read", "arguments": '{"path":"probe.txt"}'}}]


def test_indexless_parallel_calls_are_joined_by_identity():
    calls = stream_calls([
        [{"id": "a", "function": {"name": "read", "arguments": '{"path":'}},
         {"id": "b", "function": {"name": "read", "arguments": '{"path":'}}],
        [{"id": "b", "function": {"arguments": '"b"}'}},
         {"id": "a", "function": {"arguments": '"a"}'}}],
    ])
    assert [(c["id"], json.loads(c["function"]["arguments"])) for c in calls] == [
        ("a", {"path": "a"}), ("b", {"path": "b"})]


@pytest.mark.parametrize("fragment", [
    {"function": {"arguments": "}"}},
    {"index": True, "function": {}},
    {"index": 0, "id": "changed", "function": {}},
    {"index": 1, "id": "a", "function": {}},
])
def test_ambiguous_or_conflicting_fragments_fail_without_delivery(fragment):
    with pytest.raises(GenerationUnknownError, match="malformed streamed tool call"):
        stream_calls([
            [{"index": 0, "id": "a", "function": {"name": "read", "arguments": "{"}},
             {"index": 1, "id": "b", "function": {"name": "read", "arguments": "{"}}],
            [fragment],
        ])
