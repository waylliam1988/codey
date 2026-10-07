"""Partial bytes cannot keep a generation alive past its total deadline."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codey.providers.api_transport import GenerationUnknownError, generate


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_partial_bytes_without_complete_frame_are_interrupted_without_resend(content_type):
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
            for _ in range(100):
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
        started = time.monotonic()
        with pytest.raises(GenerationUnknownError):
            generate(f"http://127.0.0.1:{server.server_port}/v1/responses", {}, {}, timeout=0.15, observe=lambda **_: None)
        assert time.monotonic() - started < 1.4
        assert requests == ["/v1/responses"]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join()
