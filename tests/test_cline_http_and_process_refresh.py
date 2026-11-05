"""Loopback HTTP framing and cross-process token rotation, without live accounts."""
import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from codey.providers.cline import auth as auth_module
from codey.providers.cline.auth import Auth
from codey.providers.cline.http import HttpClient
from codey.storage.local_store import write_json_atomic
from tests.support.cline import MemoryStore, credential, token_response


@pytest.fixture
def endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/must-not-visit")
                self.end_headers()
                return
            self.respond({"data": ["chunk-" + str(i) for i in range(20000)]})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = parse_qs(raw.decode()) if "form-urlencoded" in self.headers["Content-Type"] else json.loads(raw)
            calls.append((self.path, body))
            if self.path.endswith("/device"):
                self.respond({"device_code": "DEVICE_MARKER", "user_code": "USER_MARKER",
                    "verification_uri": "https://authkit.cline.bot/device", "expires_in": 60, "interval": 1})
            elif self.path.endswith("/authenticate"):
                self.respond({"access_token": "WORKOS_ACCESS", "refresh_token": "WORKOS_REFRESH"})
            else:
                if self.path.endswith("/refresh"):
                    time.sleep(0.15)
                self.respond(token_response())

        def respond(self, body):
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            for offset in range(0, len(raw), 1000):
                self.wfile.write(raw[offset:offset + 1000])
                self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_http_reader_collects_multiple_body_chunks_and_never_follows_redirects(endpoint):
    url, _ = endpoint
    client = HttpClient()
    reply = client.request(url + "/chunked", deadline=time.monotonic() + 5)
    assert len(reply.body["data"]) == 20000
    with pytest.raises(RuntimeError, match="service request failed"):
        client.request(url + "/redirect", deadline=time.monotonic() + 5)


def test_device_login_uses_real_form_and_register_json_over_loopback(endpoint, tmp_path, monkeypatch):
    url, calls = endpoint
    monkeypatch.setattr(auth_module, "API_ORIGIN", url)
    monkeypatch.setattr(auth_module, "WORKOS_ORIGIN", url)
    store = MemoryStore()
    auth = Auth(store, HttpClient(), tmp_path / "credentials", browser=lambda _: True)
    auth.start_login()
    auth._thread.join(8)
    assert not auth._thread.is_alive() and auth.snapshot()["state"] == "ready"
    assert calls[0][1] == {"client_id": [auth_module.WORKOS_CLIENT_ID]}
    assert calls[1][1]["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
    assert calls[2][1] == {"accessToken": "WORKOS_ACCESS", "refreshToken": "WORKOS_REFRESH"}
    assert len(store.saved) == 1


def test_two_fresh_processes_coordinate_one_rotating_refresh(endpoint, tmp_path):
    url, calls = endpoint
    write_json_atomic(tmp_path / "fixture-tokens.json", asdict(credential(expires=1)))
    command = [sys.executable, "-m", "tests.support.cline_refresh_probe", str(tmp_path), url]
    children = [subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        reports = []
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stdout + stderr
            reports.append(json.loads(stdout.strip()))
        assert reports == [{"version": 1, "epoch": "account-A"}] * 2
        assert len(calls) == 1 and calls[0][0].endswith("/refresh")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)
