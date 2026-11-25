"""Real loopback connections exercise HTTP admission before request threads."""
from __future__ import annotations

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler
from time import monotonic, sleep

import pytest

from codey.app import folder_picker, server


def test_http_rejects_before_spawning_an_extra_request_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def handle(self) -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test did not release the first connection")
            super().handle()

        def do_GET(self) -> None:
            self.send_response(204)
            self.end_headers()

    monkeypatch.setattr(server, "HTTP_CONNECTIONS", 1)
    with server.CodeyHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(("127.0.0.1", httpd.server_port), timeout=3) as first:
                first.sendall(b"GET / HTTP/1.0\r\n\r\n")
                assert entered.wait(3)
                with (socket.create_connection(("127.0.0.1", httpd.server_port), timeout=3) as second,
                      http.client.HTTPResponse(second) as response):
                    response.begin()
                    assert response.status == 503
                release.set()
                with http.client.HTTPResponse(first) as response:
                    response.begin()
                    assert response.status == 204
        finally:
            release.set()
            httpd.shutdown()
            worker.join(timeout=3)



def test_browser_folder_dialog_admits_and_polls_without_waiting_for_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def run_dialog(mode: str, initial: str) -> dict[str, object]:
        assert (mode, initial) == ("open", "")
        entered.set()
        assert release.wait(3)
        return folder_picker.folder_selection(None)

    monkeypatch.setattr(folder_picker, "_run_dialog", run_dialog)
    picker = folder_picker.FolderPicker()
    try:
        status, payload = picker.start({})
        assert status == 202
        identity = payload["id"]
        assert isinstance(identity, str)
        assert entered.wait(3)
        assert picker.start({})[0] == 409
        assert picker.status(identity)[0] == 202
        assert picker.status("wrong")[0] == 404
        release.set()
        deadline = monotonic() + 3
        while picker.status(identity)[0] == 202 and monotonic() < deadline:
            sleep(0.01)
        assert picker.status(identity) == (200, folder_picker.folder_selection(None))
        status, next_payload = picker.start({})
        assert status == 202 and next_payload["id"] != identity
        assert picker.status(identity) == (200, folder_picker.folder_selection(None))
    finally:
        release.set()
        picker.close()
    assert picker.start({})[0] == 503
