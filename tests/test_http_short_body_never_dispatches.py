"""EOF must not turn a partial HTTP message into an authorized operation."""

from __future__ import annotations

import http.client
import json
import socket
from unittest import mock

import pytest

from codey.app import server
from tests.test_operator_auth_blocks_unauthenticated_http import httpd as httpd


def _post_and_half_close(httpd, path, payload, declared_length, *, cookie=""):
    host, port = httpd.server_address
    headers = [
        f"POST {path} HTTP/1.1", f"Host: {host}:{port}",
        f"Content-Length: {declared_length}", "Content-Type: application/json",
    ]
    if cookie:
        headers.append(f"Cookie: {cookie}")
    wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + payload
    with socket.create_connection(httpd.server_address, timeout=3) as connection:
        connection.sendall(wire)
        connection.shutdown(socket.SHUT_WR)
        with http.client.HTTPResponse(connection) as response:
            response.begin()
            return response.status, json.loads(response.read())


@pytest.mark.parametrize("short", [True, False])
def test_eof_dispatch_requires_the_complete_declared_body(httpd, short):
    cookie = httpd.operator_auth.exchange(httpd.operator_auth.issue_bootstrap()).split(";", 1)[0]
    route = mock.Mock(return_value=(200, {"ok": True}))
    with (
        mock.patch.dict(server._POST_ROUTES, {"/api/stop": route}),
        mock.patch.object(server, "get_state", return_value=object()) as state,
    ):
        status, body = _post_and_half_close(httpd, "/api/stop", b"{}", 3 if short else 2, cookie=cookie)
    if short:
        assert status == 400
        assert body == {"error": "incomplete request body"}
        state.assert_not_called()
        route.assert_not_called()
    else:
        assert status == 200 and body == {"ok": True}
        state.assert_called_once_with()
        assert route.call_args.args[1] == {}
        route.assert_called_once()


def test_short_bootstrap_body_does_not_consume_the_one_time_token(httpd):
    token = httpd.operator_auth.issue_bootstrap()
    payload = json.dumps({"token": token}).encode("utf-8")
    status, body = _post_and_half_close(httpd, "/api/operator_session", payload, len(payload) + 1)
    assert status == 400 and body == {"error": "incomplete request body"}
    assert httpd.operator_auth.exchange(token) is not None
