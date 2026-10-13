"""Partial bytes cannot keep a generation alive past its total deadline."""
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codey.providers import api_transport
from codey.providers.api_transport import GenerationUnknownError, generate


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_partial_bytes_without_complete_frame_are_interrupted_without_resend(content_type, monkeypatch):
    def unnecessary_tls_setup(*_args, **_kwargs):
        raise AssertionError("plain HTTP must not load TLS certificates")

    monkeypatch.setattr("ssl.create_default_context", unnecessary_tls_setup)
    stop, requests = threading.Event(), []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            # Every byte arrives within the socket timeout, but no complete
            # JSON body / SSE frame ever arrives. A socket timeout is not a
            # total generation deadline.
            while not stop.is_set():
                try:
                    self.wfile.write(b" ")
                    self.wfile.flush()
                except OSError:
                    break
                if stop.wait(0.02):
                    break

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1/responses"
        # Establish the response before starting the short body deadline.
        # Under CI/coverage, connection setup alone can exceed 150ms; that
        # correctly raises NotSent, but does not exercise partial-body reads.
        request = urllib.request.Request(endpoint, data=b"{}", method="POST")
        with api_transport.open_request(request, timeout=5) as response:
            open_calls = []

            def reuse_response(request, _timeout, **_kwargs):
                open_calls.append(request.full_url)
                return response

            monkeypatch.setattr(api_transport, "open_request", reuse_response)
            started = time.monotonic()
            with pytest.raises(GenerationUnknownError):
                generate(endpoint, {}, {}, timeout=0.15, observe=lambda **_: None)
            assert time.monotonic() - started < 1.4
            assert open_calls == [endpoint]
        assert requests == ["/v1/responses"]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join()
