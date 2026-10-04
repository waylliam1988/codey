"""Real HTTP requests cannot operate Codey without an operator session."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from unittest import mock

import pytest

from codey.app import server


@pytest.fixture
def httpd():
    instance = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def request(httpd, method, path, *, body=None, headers=None):
    payload = body.encode("utf-8") if body is not None else b""
    host, port = httpd.server_address
    fields = {"Host": f"{host}:{port}", "Accept-Encoding": "identity"}
    if body is not None:
        fields["Content-Length"] = str(len(payload))
    fields.update(headers or {})
    # These bounded fixtures are one small HTTP message. Sending headers and
    # body separately races the intentional early 401/403 and Windows close;
    # send the complete fixture without retrying a rejected request.
    lines = [f"{method} {path} HTTP/1.1", *(f"{key}: {value}" for key, value in fields.items()), "", ""]
    wire = "\r\n".join(lines).encode("latin-1") + payload
    connection = socket.create_connection(httpd.server_address, timeout=3)
    try:
        connection.sendall(wire)
        with http.client.HTTPResponse(connection) as response:
            response.begin()
            return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/shell_approval"), ("POST", "/api/run"), ("POST", "/api/stop"),
    ("GET", "/api/state"), ("GET", "/api/run_details"), ("GET", "/api/events"),
])
def test_unauthenticated_http_never_reaches_control_or_state(httpd, method, path):
    with mock.patch.object(server, "get_state", side_effect=AssertionError("unauthenticated dispatch")) as state:
        status, _, body = request(httpd, method, path, body="{}")
    assert status == 401
    assert json.loads(body)["error"] == "operator authentication required"
    state.assert_not_called()


def exchange(httpd, token):
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    return request(httpd, "POST", "/api/operator_session", body=json.dumps({"token": token}),
                   headers={"Origin": origin, "Content-Type": "application/json"})


def test_valid_bootstrap_sets_private_cookie_and_allows_real_route(httpd):
    token = httpd.operator_auth.issue_bootstrap()
    status, headers, body = exchange(httpd, token)
    assert status == 200 and json.loads(body) == {"ok": True}
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Domain=" not in cookie
    assert token not in body.decode()
    with (
        mock.patch.dict(server._GET_ROUTES, {"/api/state": lambda *_: (200, {"status": "idle"})}),
        mock.patch.object(server, "get_state", return_value=object()),
    ):
        status, _, body = request(httpd, "GET", "/api/state", headers={"Cookie": cookie.split(";", 1)[0]})
    assert status == 200 and json.loads(body) == {"status": "idle"}


def test_invalid_exchange_does_not_consume_bootstrap_and_replay_is_refused(httpd):
    token = httpd.operator_auth.issue_bootstrap()
    assert exchange(httpd, "wrong")[0] == 401
    assert exchange(httpd, token)[0] == 200
    assert exchange(httpd, token)[0] == 401


def test_cookie_does_not_authorize_a_new_server(httpd):
    status, headers, _ = exchange(httpd, httpd.operator_auth.issue_bootstrap())
    assert status == 200
    other = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    try:
        assert not other.operator_auth.authenticated(headers["Set-Cookie"])
    finally:
        other.server_close()


def test_authenticated_cookie_does_not_bypass_origin_fence(httpd):
    _, headers, _ = exchange(httpd, httpd.operator_auth.issue_bootstrap())
    with mock.patch.object(server, "get_state") as state:
        status, _, _ = request(httpd, "POST", "/api/run", body="{}", headers={
            "Cookie": headers["Set-Cookie"], "Origin": "http://evil.example",
        })
    assert status == 403
    state.assert_not_called()


@pytest.mark.parametrize("path", ["/", "/icon.ico", "/assets/sse.js"])
def test_public_boot_assets_do_not_require_operator_session(httpd, path):
    assert request(httpd, "GET", path)[0] == 200
