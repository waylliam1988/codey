"""A TLS reconnect is safe only while no generation request bytes were sent."""

import http.client
import ssl
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from codey.providers import api_transport


@pytest.mark.parametrize("failure_phase", ["handshake", "after_headers"])
def test_tls_eof_reconnects_only_before_generation_submission(monkeypatch, failure_phase):
    connections = []
    sent = []
    response = SimpleNamespace(code=200, msg="OK", info=lambda: {})

    def connect(connection):
        connections.append(connection)
        if len(connections) == 1 and failure_phase == "handshake":
            raise ssl.SSLEOFError("deterministic TLS handshake EOF")
        connection.sock = type("Socket", (), {"sendall": lambda self, data: sent.append(data)})()

    def do_open(handler, connection_type, request, **kwargs):
        connection = connection_type(request.host, timeout=request.timeout)
        try:
            connection.send(b"POST /responses HTTP/1.1\r\n")
            if failure_phase == "after_headers":
                raise ssl.SSLEOFError("deterministic EOF after HTTP headers")
            connection.send(request.data)
        except OSError as exc:
            raise urllib.error.URLError(exc) from exc
        return response

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "do_open", do_open)
    request = urllib.request.Request("https://fixture.test/responses", data=b'{"model":"fixture"}')
    if failure_phase == "handshake":
        assert api_transport.open_request(request, 10) is response
        assert len(connections) == 2
        assert sent == [b"POST /responses HTTP/1.1\r\n", b'{"model":"fixture"}']
    else:
        with pytest.raises(urllib.error.URLError):
            api_transport.open_request(request, 10)
        assert len(connections) == 1


def test_persistent_handshake_failure_is_bounded_and_explicitly_not_sent(monkeypatch):
    connections = []

    def connect(connection):
        connections.append(connection)
        raise ssl.SSLEOFError("deterministic TLS handshake EOF")

    def do_open(handler, connection_type, request, **kwargs):
        try:
            connection_type(request.host).send(b"POST")
        except OSError as exc:
            raise urllib.error.URLError(exc) from exc

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    monkeypatch.setattr(urllib.request.HTTPSHandler, "do_open", do_open)
    with pytest.raises(api_transport.GenerationNotSentError):
        api_transport.open_request(urllib.request.Request("https://fixture.test/responses", data=b"{}"), 10)
    assert len(connections) == 2
