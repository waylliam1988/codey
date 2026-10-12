"""Regression locks for pre-submit cancellation and one-shot transport attempts."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codey.providers import api_transport


def test_cancelled_before_connect_is_classified_not_sent() -> None:
    requests: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: object) -> None:
            return None

        def do_POST(self) -> None:
            requests.append(1)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(api_transport.GenerationNotSentError):
            api_transport.generate(
                f"http://127.0.0.1:{server.server_port}/v1",
                {}, {}, timeout=1,
                observe=lambda **_: None,
                on_control=lambda control: control.cancel(),
            )
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
