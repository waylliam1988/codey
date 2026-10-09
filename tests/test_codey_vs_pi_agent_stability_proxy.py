"""The observer forwards HTTP/SSE faithfully and binds requests at submission."""

import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.manual.codey_vs_pi_agent_stability_ab import _Proxy


@contextmanager
def servers(mode):
    arrived, release = threading.Event(), threading.Event()
    bodies = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self.send_response(503 if mode == "error" else 200)
            self.send_header("Content-Type", "text/event-stream" if mode == "stream" else "application/json")
            self.end_headers()
            if mode == "stream":
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
                self.wfile.flush()
            arrived.set()
            if mode in {"stream", "hold"}:
                release.wait(3)
            self.wfile.write(b'data: [DONE]\n\n' if mode == "stream" else b'{"error":"busy"}')

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    proxy = _Proxy(("127.0.0.1", 0), f"http://127.0.0.1:{upstream.server_port}", 5)
    proxy.active_arm = "submitted"
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield proxy, arrived, release, bodies
    finally:
        release.set()
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()


def request(proxy, body=b'{}'):
    return urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"}), timeout=3)


def test_proxy_preserves_upstream_rejection_status():
    with servers("error") as (proxy, _, _, _), pytest.raises(urllib.error.HTTPError) as caught:
        request(proxy)
    assert caught.value.code == 503


def test_proxy_does_not_rewrite_a_request_after_admission():
    with servers("error") as (proxy, _, _, bodies):
        body = b'{"temperature":0.5,"max_tokens":1024,"seed":17}'
        with pytest.raises(urllib.error.HTTPError):
            request(proxy, body)
        assert json.loads(bodies[0]) == json.loads(body)


def test_proxy_delivers_first_sse_frame_before_generation_finishes():
    with servers("stream") as (proxy, arrived, release, _):
        first = threading.Event()
        def read():
            with request(proxy) as response:
                if b"first" in response.readline():
                    first.set()
                response.read()
        thread = threading.Thread(target=read)
        thread.start()
        assert arrived.wait(2)
        delivered = first.wait(.5)
        release.set()
        thread.join(4)
        assert delivered


def test_proxy_keeps_the_arm_that_submitted_the_request():
    with servers("hold") as (proxy, arrived, release, _):
        thread = threading.Thread(target=lambda: request(proxy).read())
        thread.start()
        assert arrived.wait(2)
        proxy.active_arm = "next-arm"
        release.set()
        thread.join(4)
        assert proxy.records[0]["arm"] == "submitted"


def test_inflight_generation_is_visible_before_a_crashed_client_can_hide_its_cost():
    with servers("hold") as (proxy, arrived, release, _):
        thread = threading.Thread(target=lambda: request(proxy).read())
        thread.start()
        assert arrived.wait(2)
        pending = bool(proxy.records and proxy.records[0].get("response_complete") is False)
        release.set()
        thread.join(4)
        assert pending
        assert len(proxy.records) == 1
        assert proxy.records[0]["response_complete"] is True
