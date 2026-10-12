"""Actual http.client send boundaries distinguish handshake EOF from POST EOF."""

import http.client
import ssl
import urllib.request

import pytest

from codey.providers import api_transport
from codey.providers.api_http import ApiHttpSession


@pytest.mark.parametrize("phase", ["handshake", "first_write", "body_write"])
def test_pooled_tls_eof_classification_at_actual_send_boundary(monkeypatch, phase):
    writes = []

    class Socket:
        def sendall(self, data):
            writes.append(data)
            if phase == "first_write" or (phase == "body_write" and len(writes) == 2):
                raise ssl.SSLEOFError("injected write EOF")

        def close(self):
            pass

        def shutdown(self, _how):
            pass

    def connect(connection):
        if phase == "handshake":
            raise ssl.SSLEOFError("injected handshake EOF")
        connection.sock = Socket()

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    session = ApiHttpSession()
    control = api_transport.AttemptControl()
    error = api_transport.GenerationNotSentError if phase == "handshake" else api_transport.GenerationUnknownError
    try:
        with pytest.raises(error) as caught:
            api_transport.open_request(
                urllib.request.Request("https://fixture.test/v1/responses", data=b"{}", method="POST"), 1,
                control=control, http_session=session,
            )
        assert caught.value.reason_code == (
            "pre_submission_tls_eof" if phase == "handshake" else "post_submission_tls_eof"
        )
        assert len(writes) == {"handshake": 0, "first_write": 1, "body_write": 2}[phase]
        assert control._connections == [], "pooled attempts must never register naked connections"
    finally:
        control.cancel()
        session.release()
