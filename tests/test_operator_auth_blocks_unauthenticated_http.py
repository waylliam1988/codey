"""Real HTTP requests cannot operate Codey without an operator session."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from codey.app import server
from codey.app.context import AppContext
from codey.app.http_plumbing import WEB_DIR
from codey.types.json_value import JsonObject, parse_json_object
from codey.utils.object_fields import object_fields


@pytest.fixture()
def httpd() -> Iterator[server.CodeyHTTPServer]:
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


class ListeningServer(Protocol):
    @property
    def server_address(
        self,
    ) -> tuple[str | bytes | bytearray, int] | tuple[str | bytes | bytearray, int, int, int]: ...


Response = tuple[int, dict[str, str], bytes]


def request(
    httpd: ListeningServer, method: str, path: str, *,
    body: str | None = None, headers: dict[str, str] | None = None,
) -> Response:
    payload = body.encode("utf-8") if body is not None else b""
    address = httpd.server_address
    assert len(address) == 2
    host, port = address
    assert isinstance(host, str)
    fields = {"Host": f"{host}:{port}", "Accept-Encoding": "identity"}
    if body is not None:
        fields["Content-Length"] = str(len(payload))
    fields.update(headers or {})
    # These bounded fixtures are one small HTTP message. Sending headers and
    # body separately races the intentional early 401/403 and Windows close;
    # send the complete fixture without retrying a rejected request.
    lines = [f"{method} {path} HTTP/1.1", *(f"{key}: {value}" for key, value in fields.items()), "", ""]
    wire = "\r\n".join(lines).encode("latin-1") + payload
    connection = socket.create_connection((host, port), timeout=3)
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
    ("GET", "/api/connection_auth?source=fixture"), ("POST", "/api/connection_action"),
])
def test_unauthenticated_http_never_reaches_control_or_state(
    httpd: server.CodeyHTTPServer, method: str, path: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[server.Handler] = []

    def state(handler: server.Handler) -> AppContext:
        calls.append(handler)
        raise AssertionError("unauthenticated dispatch")

    monkeypatch.setattr(server.Handler, "_get_state", state)
    status, _, body = request(httpd, method, path, body="{}")
    assert status == 401
    assert parse_json_object(body.decode())["error"] == "operator authentication required"
    assert calls == []


def exchange(httpd: server.CodeyHTTPServer, token: str) -> Response:
    origin = f"http://127.0.0.1:{httpd.server_address[1]}"
    payload: JsonObject = {"token": token}
    return request(httpd, "POST", "/api/operator_session", body=json.dumps(payload),
                   headers={"Origin": origin, "Content-Type": "application/json"})


def test_valid_bootstrap_sets_private_cookie_and_allows_real_route(
    httpd: server.CodeyHTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = httpd.operator_auth.issue_bootstrap()
    status, headers, body = exchange(httpd, token)
    assert status == 200 and parse_json_object(body.decode()) == {"ok": True}
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Domain=" not in cookie
    assert token not in body.decode()
    def state(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, JsonObject]:
        return 200, {"status": "idle"}

    routes: object = server._GET_ROUTES
    assert object_fields(routes)
    monkeypatch.setitem(routes, "/api/state", state)
    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    monkeypatch.setattr(httpd, "app_context", ctx)
    try:
        status, _, body = request(httpd, "GET", "/api/state", headers={"Cookie": cookie.split(";", 1)[0]})
        assert status == 200 and parse_json_object(body.decode()) == {"status": "idle"}
    finally:
        ctx.close()


def test_invalid_exchange_does_not_consume_bootstrap_and_replay_is_refused(httpd: server.CodeyHTTPServer) -> None:
    token = httpd.operator_auth.issue_bootstrap()
    assert exchange(httpd, "wrong")[0] == 401
    assert exchange(httpd, token)[0] == 200
    assert exchange(httpd, token)[0] == 401


def test_cookie_does_not_authorize_a_new_server(httpd: server.CodeyHTTPServer) -> None:
    status, headers, _ = exchange(httpd, httpd.operator_auth.issue_bootstrap())
    assert status == 200
    other = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
    try:
        assert not other.operator_auth.authenticated(headers["Set-Cookie"])
    finally:
        other.server_close()


def test_authenticated_cookie_does_not_bypass_origin_fence(
    httpd: server.CodeyHTTPServer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, headers, _ = exchange(httpd, httpd.operator_auth.issue_bootstrap())
    calls: list[bool] = []

    def state() -> AppContext:
        calls.append(True)
        raise AssertionError("foreign origin must not access application state")

    monkeypatch.setattr("tests.http_context.get_context", state)
    status, _, _ = request(httpd, "POST", "/api/run", body="{}", headers={
        "Cookie": headers["Set-Cookie"], "Origin": "http://evil.example",
    })
    assert status == 403
    assert calls == []


def _published_bundle_path() -> str:
    bundles = sorted((WEB_DIR / "assets").glob("*.js"))
    assert bundles
    return "/assets/" + bundles[0].name


@pytest.mark.parametrize("path", ["/", "/icon.ico"])
def test_public_boot_assets_do_not_require_operator_session(httpd: server.CodeyHTTPServer, path: str) -> None:
    assert request(httpd, "GET", path)[0] == 200


def test_published_bundle_does_not_require_operator_session(httpd: server.CodeyHTTPServer) -> None:
    assert request(httpd, "GET", _published_bundle_path())[0] == 200
