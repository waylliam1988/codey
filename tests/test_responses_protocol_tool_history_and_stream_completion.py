"""Responses wire format, exact call identity, reasoning replay, and terminal SSE."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationUnknownError
from codey.providers.base import ProviderToolDefinition, ProviderToolResult, TurnFinish

READ = ProviderToolDefinition("read", "Read a permitted file", {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
})


@contextmanager
def service(replies):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            content_type, body = replies.pop(0)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for start in range(0, len(body), 7):
                self.wfile.write(body[start:start + 7])
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def response(output, status="completed"):
    return {"id": "resp_fixture", "status": status, "output": output}


def wire(body, stream=False):
    if stream:
        return "text/event-stream", ("data: " + json.dumps({"type": "response." + body["status"], "response": body}) + "\n\n").encode()
    return "application/json", json.dumps(body).encode()


@pytest.mark.parametrize("stream", [False, True])
def test_responses_preserves_call_id_reasoning_and_neutral_result(stream):
    reasoning = {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": "opaque-fixture"}
    call = {"type": "function_call", "id": "fc_item_not_call", "call_id": "call_exact", "name": "read", "arguments": '{"path":"fixture.txt"}'}
    answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "received"}]}
    with service([wire(response([reasoning, call]), stream), wire(response([answer]), stream)]) as (url, requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses", stream=stream)
        turn = provider.send_turn("read the fixture", [READ])
        assert turn.tool_calls[0].id == "call_exact"
        assert turn.tool_calls[0].arguments == {"path": "fixture.txt"}
        final = provider.send_tool_results([ProviderToolResult("call_exact", "OK: fixture")], [READ])
        assert final.text == "received"
        first = requests[0][2]
        assert requests[0][0] == "/v1/responses"
        assert first["store"] is False
        assert first["tools"][0]["name"] == "read"
        assert first["tools"][0]["strict"] is False
        assert "messages" not in first
        second = requests[1][2]["input"]
        assert reasoning in second
        assert call in second
        assert {"type": "function_call_output", "call_id": "call_exact", "output": "OK: fixture"} in second


def test_sse_without_terminal_event_is_unknown_and_never_commits_partial_call():
    partial = b'data: {"type":"response.function_call_arguments.delta","delta":"{\\\"path\\\":"}\n\n'
    with service([("text/event-stream", partial)]) as (url, requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses", stream=True)
        with pytest.raises(GenerationUnknownError):
            provider.send_turn("read", [READ])
        assert len(requests) == 1
        assert provider._messages == []


def test_incomplete_tool_arguments_never_become_an_executable_call():
    call = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "read", "arguments": '{"path":'}
    body = response([call], "incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    with service([wire(body)]) as (url, _requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses")
        with pytest.raises(RuntimeError):
            provider.send_turn("read", [READ])
        assert provider._messages == []


def test_text_output_limit_is_normalized_without_raw_decision_fields():
    body = response([{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}], "incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    with service([wire(body)]) as (url, _requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses")
        turn = provider.send_turn("continue", [READ])
        assert turn.finish is TurnFinish.OUTPUT_LIMIT
        assert turn.tool_calls == ()


@pytest.mark.parametrize("stream", [False, True])
def test_plain_responses_output_limit_cannot_execute_text_json_or_commit_history(stream):
    from codey.providers.error_classification import OutputLengthError

    body = response([{"type": "message", "content": [{"type": "output_text", "text": '{"tool":"done","args":{"summary":"premature"}}'}]}], "incomplete")
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    with service([wire(body, stream)]) as (url, requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses", native_tools=False, stream=stream)
        with pytest.raises(OutputLengthError):
            provider.send("Complete the task")
        assert provider._messages == []
        assert len(requests) == 1


def test_responses_replays_assistant_message_after_call_before_its_result():
    call = {"type": "function_call", "call_id": "call1", "name": "read", "arguments": '{"path":"fixture.txt"}'}
    commentary = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Reading now."}]}
    final = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Read."}]}
    with service([wire(response([call, commentary])), wire(response([final]))]) as (url, requests):
        provider = ApiProvider(url, "fixture", api_protocol="openai-responses")
        provider.send_turn("read the fixture", [READ])
        assert provider.send_tool_results([ProviderToolResult("call1", "contents")], [READ]).text == "Read."
        assert requests[1][2]["input"][1:] == [call, commentary, {"type": "function_call_output", "call_id": "call1", "output": "contents"}]
