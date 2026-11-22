"""Failed HTTP listener construction must leave no owned socket behind."""

from socket import socket

import pytest

from codey.app import server


def test_server_validation_failure_does_not_leak_a_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    bound: list[socket] = []
    bind = server.CodeyHTTPServer.server_bind

    def record_bind(instance: server.CodeyHTTPServer) -> None:
        bind(instance)
        bound.append(instance.socket)

    def invalid_assets() -> None:
        raise ValueError("missing frontend assets")

    monkeypatch.setattr(server.CodeyHTTPServer, "server_bind", record_bind)
    monkeypatch.setattr(server, "validate_web_root", invalid_assets)
    try:
        with pytest.raises(ValueError, match="missing frontend assets"):
            server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        assert all(listener.fileno() == -1 for listener in bound), "failed construction leaked a listener"
    finally:
        for listener in bound:
            listener.close()


