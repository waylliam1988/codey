"""Real loopback connections exercise HTTP admission before request threads."""
from __future__ import annotations

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

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



def test_browser_folder_dialog_runs_off_the_handler_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    picker_entered = threading.Event()
    release = threading.Event()
    draw_threads: list[threading.Thread] = []
    selected: list[str | None] = []
    caller = threading.Thread(
        target=lambda: selected.append(folder_picker.pick_folder(initial=str(tmp_path))),
    )

    def choose(mode: str, initial: str | None) -> str | None:
        assert mode == "open" and initial == str(tmp_path)
        draw_threads.append(threading.current_thread())
        picker_entered.set()
        assert release.wait(3)
        return str(tmp_path)

    monkeypatch.setattr(folder_picker, "_choose_folder", choose)
    caller.start()
    try:
        assert picker_entered.wait(3)
        with pytest.raises(folder_picker.FolderPickerBusy):
            folder_picker.pick_folder()
    finally:
        release.set()
        caller.join(timeout=3)
    assert draw_threads and draw_threads[0] is not caller
    assert selected == [str(tmp_path)]
