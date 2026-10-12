"""Stop cancels a request while it is waiting for response headers."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from codey.providers import api_transport


def test_attempt_control_interrupts_waiting_response_header(monkeypatch):
    controls = []
    entered = threading.Event()
    closed = threading.Event()

    class Blocked:
        def __enter__(self):
            entered.set()
            while not controls[0].cancelled:
                time.sleep(0.005)
            closed.set()
            raise api_transport.GenerationUnknownError("cancelled")
        def __exit__(self, *args):
            return None

    monkeypatch.setattr(api_transport, "open_request", lambda request, timeout, **kwargs: Blocked())
    errors = []
    def run():
        try:
            api_transport.generate("http://fixture", {}, {}, timeout=5, observe=lambda **_: None,
                                   on_control=lambda value: controls.append(value))
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(1)
    controls[0].cancel()
    worker.join(1)
    assert errors and isinstance(errors[0], api_transport.GenerationUnknownError)
    assert entered.is_set() and closed.is_set()


def test_cancelled_before_connect_sends_no_post():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return None
        def do_POST(self):
            requests.append(1)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(api_transport.GenerationNotSentError):
            api_transport.generate(f"http://127.0.0.1:{server.server_port}/v1", {}, {}, timeout=1,
                                   observe=lambda **_: None,
                                   on_control=lambda control: control.cancel())
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
