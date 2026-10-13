"""Real fetch I/O retains shared framing, rejection and Stop guarantees."""
import json
import threading
import time

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import (
    AttemptControl,
    GenerationNotSentError,
    GenerationRejectedError,
    GenerationUnknownError,
    generate,
)
from codey.providers.zen import node_http
from tests.test_api_transport_real_stream_and_pool_lifecycle import service
from tests.test_responses_protocol_tool_history_and_stream_completion import service as reply_service


@pytest.mark.parametrize("stream", [False, True])
def test_fetch_uses_shared_tool_parser_and_releases_process(monkeypatch, stream):
    message = {"tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "read", "arguments": '{"path":"probe.txt"}'}}]}
    if stream:
        body = ('data: ' + json.dumps({"choices": [{"delta": message, "finish_reason": "tool_calls"}]})
                + '\n\ndata: [DONE]\n\n').encode()
        reply = ("text/event-stream", body)
    else:
        reply = ("application/json", json.dumps({"choices": [{"message": message, "finish_reason": "tool_calls"}]}).encode())
    processes = []
    real_popen = node_http.subprocess.Popen
    def launch(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(node_http.subprocess, "Popen", launch)
    with reply_service([reply]) as (url, requests):
        monkeypatch.setattr(node_http, "ZEN_BASE_URL", url)
        body = generate(url + "/chat/completions", {}, {}, timeout=5, observe=lambda **_: None,
                        transport_options={"opener": node_http.open_request})
    assert body["choices"][0]["message"]["tool_calls"][0]["id"] == "c1"
    assert len(requests) == 1 and len(processes) == 1
    assert processes[0].poll() is not None


@pytest.mark.parametrize("mode", ["headers", "error_silent"])
def test_fetch_stop_interrupts_headers_and_rejection_without_resend(monkeypatch, mode):
    with service(mode) as (url, requests, _, received):
        monkeypatch.setattr(node_http, "ZEN_BASE_URL", url)
        provider = ApiProvider(url, "fixture", timeout=5, transport_options={"opener": node_http.open_request})
        errors = []
        def send():
            try:
                provider.send("hello")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        assert received.wait(3)
        started = time.monotonic()
        provider.abandon_inflight()
        thread.join(1)
        assert not thread.is_alive() and time.monotonic() - started < 1
        assert len(requests) == 1 and isinstance(errors[0], GenerationUnknownError)
        provider.close()


def test_fetch_complete_rejection_keeps_service_type(monkeypatch):
    with service("slow_error") as (url, requests, _, _):
        monkeypatch.setattr(node_http, "ZEN_BASE_URL", url)
        with pytest.raises(GenerationRejectedError) as error:
            generate(url + "/chat/completions", {}, {}, timeout=5, observe=lambda **_: None,
                     transport_options={"opener": node_http.open_request})
        assert error.value.status == 429 and error.value.error_type == "RateLimitError"
        assert len(requests) == 1


def test_fetch_connection_refused_is_proven_not_submitted(monkeypatch):
    import socket
    import urllib.request

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1"
    monkeypatch.setattr(node_http, "ZEN_BASE_URL", url)
    control = AttemptControl()
    request = urllib.request.Request(url + "/chat/completions", data=b"{}", method="POST")
    with pytest.raises(GenerationNotSentError) as error:
        node_http.open_request(request, 3, control=control)
    assert error.value.reason_code == "pre_submission_network_error"
    assert control.submission_started is False


def test_stop_before_async_submission_receipt_is_unknown_when_peer_received_post(monkeypatch):
    release = threading.Event()
    original = node_http.NodeResponse.frame
    def delayed_frame(response):
        frame = original(response)
        if frame.get("type") == "submitted":
            release.wait(3)
        return frame
    monkeypatch.setattr(node_http.NodeResponse, "frame", delayed_frame)
    with service("headers") as (url, requests, _, received):
        monkeypatch.setattr(node_http, "ZEN_BASE_URL", url)
        provider = ApiProvider(url, "fixture", timeout=5, transport_options={"opener": node_http.open_request})
        errors = []
        def send():
            try:
                provider.send("hello")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        try:
            assert received.wait(3)
            provider.abandon_inflight()
            thread.join(1)
            assert not thread.is_alive() and len(requests) == 1
            assert isinstance(errors[0], GenerationUnknownError)
        finally:
            release.set()
            thread.join(1)
            provider.close()
