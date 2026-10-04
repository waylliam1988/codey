"""The small auth fixture sends headers and body before an early rejecting peer."""
import io

from tests.test_operator_auth_blocks_unauthenticated_http import request


def test_fixture_does_not_send_body_after_peer_can_reject_headers(monkeypatch):
    class Peer:
        def __init__(self):
            self.writes = []
            self.closed = False

        def sendall(self, data):
            # Deterministic form of the Windows early-close race: once a
            # header-only write allows a 401, a later body can hit a reset.
            if not data.endswith(b"\r\n\r\n{}"):
                raise ConnectionAbortedError("peer rejected before body arrived")
            self.writes.append(data)

        def makefile(self, *_args):
            return io.BytesIO(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 2\r\n\r\n{}")

        def close(self):
            self.closed = True

    peer = Peer()
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: peer)
    httpd = type("Server", (), {"server_address": ("127.0.0.1", 1234)})()
    status, _, body = request(httpd, "GET", "/api/run_details", body="{}")
    assert status == 401 and body == b"{}"
    assert len(peer.writes) == 1 and peer.closed
