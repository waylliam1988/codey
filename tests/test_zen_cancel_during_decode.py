"""Zen's real factory/fetch path must reject a Stop arriving during decoding."""
import copy
import json
import threading
import urllib.request
from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from codey.providers.api_transport import GenerationUnknownError
from codey.providers.base import ProviderToolDefinition
from codey.providers.zen import connection, node_http
from codey.providers.zen.catalog import ZenCatalog
from codey.runtime.core import cancellation
from tests.test_responses_protocol_tool_history_and_stream_completion import service


def wire(protocol, stream, *, tool=False):
    arguments = json.dumps({"path": "late.txt"})
    if protocol == "openai-responses":
        item = ({"type": "function_call", "id": "item-late", "call_id": "call-late", "status": "completed",
                 "name": "read", "arguments": arguments} if tool else
                {"type": "message", "role": "assistant", "status": "completed",
                 "content": [{"type": "output_text", "text": "accepted"}]})
        body = {"status": "completed", "output": [item]}
        events = [{"type": "response.completed", "response": body}]
    else:
        message = ({"content": "", "tool_calls": [{"id": "call-late", "type": "function",
                    "function": {"name": "read", "arguments": arguments}}]} if tool else {"content": "accepted"})
        finish = "tool_calls" if tool else "stop"
        body = {"choices": [{"message": message, "finish_reason": finish}]}
        events = [{"choices": [{"index": 0, "delta": message, "finish_reason": finish}]}]
    if not stream:
        return "application/json", json.dumps(body).encode()
    raw = b"".join(("data: " + json.dumps(event) + "\n\n").encode() for event in events)
    if protocol == "openai-completions":
        raw += b"data: [DONE]\n\n"
    return "text/event-stream", raw


def open_fixture(tmp_path, monkeypatch, protocol, stream, url):
    directory = {"opencode": {"api": "https://opencode.ai/inference/openai/v1",
        "npm": "@ai-sdk/openai" if protocol == "openai-responses" else "@ai-sdk/openai-compatible",
        "models": {"cancel-fixture": {"tool_call": True, "cost": {"input": 0, "output": 0},
                                    "limit": {"context": 32768, "output": 4096}}}}}
    catalog = ZenCatalog(tmp_path, fetch=lambda *_: (directory, "fixture"))
    monkeypatch.setattr(connection, "catalog", lambda: catalog)
    monkeypatch.setattr(node_http, "validate_generation_url", lambda value: url if value.startswith(url + "/") else
                        (_ for _ in ()).throw(ValueError("unexpected test destination")))

    def loopback(request, timeout, **kwargs):
        # Only this test redirects the approved endpoint to its local HTTP peer.
        local = urllib.request.Request(url + "/" + urlsplit(request.full_url).path.rsplit("/", 1)[-1],
            data=request.data, headers=dict(request.header_items()), method=request.get_method())
        return node_http.open_request(local, timeout, **kwargs)

    monkeypatch.setattr(connection, "open_zen_request", loopback)
    selection = replace(connection.capture_selection({"model": "cancel-fixture"}), stream=stream)
    return connection.open_selection(selection)


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("late_result", ["text", "tool", "decode_error"])
def test_zen_stop_during_decode_keeps_committed_history_and_returns_no_late_tools(
    tmp_path, monkeypatch, protocol, stream, late_result,
):
    replies = [wire(protocol, stream), wire(protocol, stream, tool=late_result == "tool")]
    with service(replies) as (url, requests):
        provider = open_fixture(tmp_path, monkeypatch, protocol, stream, url)
        runtime = provider.runtime
        tools = [ProviderToolDefinition("read_file", "Read", {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]})]
        stop = threading.Event()
        try:
            provider.bind_context("cancel-fixture")
            assert provider.send_turn("Keep the accepted history.", tools).text == "accepted"
            before = copy.deepcopy(runtime.context_ledger.view)
            revision = runtime.context_ledger.revision
            generation = runtime._generation
            original = runtime._codec.decode_exchange
            decoded = []

            def decode(*args, **kwargs):
                result = original(*args, **kwargs)
                decoded.append(result[0])
                stop.set()
                if late_result == "decode_error":
                    raise ValueError("malformed late response")
                return result

            monkeypatch.setattr(runtime._codec, "decode_exchange", decode)
            with cancellation.scope(stop), pytest.raises(GenerationUnknownError, match="late response discarded"):
                provider.send_turn("This request is stopped during decoding.", tools)
            assert len(decoded) == 1
            assert bool(decoded[0].tool_calls) is (late_result == "tool")
            assert runtime._generation == generation  # Stop did not call abandon_inflight.
            assert runtime.context_ledger.view == before and runtime._messages == before
            assert runtime.context_ledger.revision == revision
            assert len(requests) == 2  # One accepted request and one stopped request; no retry.
            assert runtime.request_policy.routes._routes == {}
            assert runtime._pending_request is None and runtime._active_response is None
        finally:
            provider.close()
