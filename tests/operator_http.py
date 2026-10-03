"""Existing HTTP behavior tests authenticate through the real exchange route."""

from __future__ import annotations

import http.client
import json


class OperatorHTTPConnection(http.client.HTTPConnection):
    def __init__(self, httpd, *, timeout=5, connect_host=None):
        host, port = httpd.server_address
        super().__init__(connect_host or host, port, timeout=timeout)
        self._operator_cookie = ""
        super().request("POST", "/api/operator_session",
                        body=json.dumps({"token": httpd.operator_auth.issue_bootstrap()}),
                        headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}",
                                 "Host": f"{host}:{port}"})
        response = self.getresponse()
        response.read()
        assert response.status == 200, "operator bootstrap must succeed before testing business routes"
        self._operator_cookie = (response.getheader("Set-Cookie") or "").split(";", 1)[0]
        assert self._operator_cookie

    def request(self, method, url, body=None, headers=None, *, encode_chunked=False):
        supplied = {"Cookie": self._operator_cookie, **(headers or {})}
        return super().request(method, url, body, supplied, encode_chunked=encode_chunked)
