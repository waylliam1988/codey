"""VPN-first routing shares the request budget, history and cancellation boundary."""
import contextlib
import copy
import socket
import struct
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationNotSentError, GenerationRejectedError, GenerationUnknownError
from codey.providers.zen import direct, routing
from codey.providers.zen.connection import ZenProvider
from codey.providers.zen.reliability import ZenRequestCoordinator, ZenRequestPolicy, make_control_scope
from codey.runtime.core import cancellation


@pytest.fixture
def routed(monkeypatch):
    tunnels = []

    class Tunnel:
        def __init__(self, host, deadline, cancelled, **kwargs):
            self.proxy_url = "http://codey:private@127.0.0.1:12345"
            self.closed = False
            tunnels.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(routing, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(routing, "DirectTunnel", Tunnel)
    monkeypatch.setattr(routing, "discover_interface", lambda *_: object())
    routes = routing.ZenRoutes()
    policy = ZenRequestPolicy(ZenRequestCoordinator(), make_control_scope("https://opencode.ai/inference/openai/v1", "public", "fixture"), routes=routes)
    options = {"request_id_headers": ("X-OpenCode-Log-Id",)}
    provider = ApiProvider("https://opencode.ai/inference/openai/v1", "fixture", request_policy=policy,
                           transport_options=options, timeout=2)
    try:
        yield provider, tunnels, routes, options
    finally:
        provider.close()


def quota():
    return GenerationRejectedError(429, '{"error":{"type":"FreeUsageLimitError","message":"quota"}}')


def reply():
    return {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}


def test_each_logical_request_starts_on_vpn_and_falls_back_once_without_duplicate_history(routed, monkeypatch):
    provider, tunnels, routes, original_options = routed
    attempts = []

    def generate(endpoint, payload, headers, **kwargs):
        attempts.append((copy.deepcopy(payload), dict(kwargs["transport_options"])))
        if "fetch_proxy" not in kwargs["transport_options"]:
            raise quota()
        return reply()

    monkeypatch.setattr(api_generation, "generate", generate)
    assert provider.send("one") == "OK"
    assert routes._routes == {} and tunnels[0].closed
    assert provider.send("two") == "OK"
    assert ["fetch_proxy" in options for _, options in attempts] == [False, True, False, True]
    assert attempts[0][0] == attempts[1][0] and attempts[2][0] == attempts[3][0]
    assert len([message for message in provider._messages if message.get("role") == "assistant"]) == 2
    assert original_options == {"request_id_headers": ("X-OpenCode-Log-Id",)}
    assert len(tunnels) == 2 and all(tunnel.closed for tunnel in tunnels)
    assert routes._routes == {}


def test_successful_vpn_request_never_opens_a_direct_tunnel(routed, monkeypatch):
    provider, tunnels, routes, _ = routed
    monkeypatch.setattr(api_generation, "generate", lambda *args, **kwargs: reply())
    assert provider.send("hello") == "OK"
    assert tunnels == [] and routes._routes == {}


@pytest.mark.parametrize("failure", [
    GenerationUnknownError("peer disconnected after receiving the request"),
    GenerationRejectedError(401, '{"error":{"type":"AuthError"}}'),
    GenerationRejectedError(403, '{"error":{"type":"FreeTierError"}}'),
    GenerationNotSentError("runtime missing"),
])
def test_unknown_generation_authentication_and_missing_runtime_never_fall_back(routed, monkeypatch, failure):
    provider, tunnels, routes, _ = routed
    attempts = []

    def generate(*args, **kwargs):
        attempts.append(kwargs)
        raise failure

    monkeypatch.setattr(api_generation, "generate", generate)
    with pytest.raises(type(failure)):
        provider.send("hello")
    assert len(attempts) == 1 and tunnels == [] and routes._routes == {}


def test_direct_quota_is_terminal_and_does_not_rotate_routes(routed, monkeypatch):
    provider, tunnels, routes, _ = routed
    attempts = []

    def generate(*args, **kwargs):
        attempts.append(kwargs)
        raise quota()

    monkeypatch.setattr(api_generation, "generate", generate)
    with pytest.raises(GenerationRejectedError):
        provider.send("hello")
    assert len(attempts) == 2 and len(tunnels) == 1 and tunnels[0].closed
    assert routes._routes == {}


def test_proven_unsubmitted_network_failure_can_use_direct_route(routed, monkeypatch):
    provider, tunnels, _, _ = routed
    attempts = []

    def generate(*args, **kwargs):
        attempts.append(kwargs["transport_options"])
        if len(attempts) == 1:
            raise GenerationNotSentError("connection refused", reason_code="pre_submission_network_error")
        return reply()

    monkeypatch.setattr(api_generation, "generate", generate)
    assert provider.send("hello") == "OK"
    assert len(attempts) == 2 and tunnels[0].closed


def test_stop_during_decode_after_direct_reply_discards_history_and_closes_tunnel(routed, monkeypatch):
    runtime, tunnels, routes, original_options = routed
    provider = ZenProvider(runtime)
    stop = threading.Event()
    attempts = []

    def generate(*args, **kwargs):
        attempts.append(dict(kwargs["transport_options"]))
        if "fetch_proxy" not in kwargs["transport_options"]:
            raise quota()
        return reply()

    original = runtime._codec.decode_exchange

    def decode(*args, **kwargs):
        result = original(*args, **kwargs)
        stop.set()
        return result

    monkeypatch.setattr(api_generation, "generate", generate)
    monkeypatch.setattr(runtime._codec, "decode_exchange", decode)
    generation = runtime._generation
    with cancellation.scope(stop), pytest.raises(GenerationUnknownError, match="late response discarded"):
        provider.send("Stop the direct-route reply")
    assert ["fetch_proxy" in options for options in attempts] == [False, True]
    assert runtime._generation == generation and not runtime.context_ledger.view
    assert len(tunnels) == 1 and tunnels[0].closed and routes._routes == {}
    assert original_options == {"request_id_headers": ("X-OpenCode-Log-Id",)}


def test_stop_on_vpn_quota_prevents_physical_fallback(routed, monkeypatch):
    provider, tunnels, routes, _ = routed
    stop = threading.Event()
    attempts = []

    def generate(*args, **kwargs):
        attempts.append(kwargs)
        stop.set()
        raise quota()

    monkeypatch.setattr(api_generation, "generate", generate)
    with cancellation.scope(stop), pytest.raises(GenerationRejectedError):
        provider.send("Do not start a fallback after Stop")
    assert len(attempts) == 1 and tunnels == [] and routes._routes == {}
    assert not provider.context_ledger.view


def test_physical_dns_rejects_fake_ip_and_truncated_responses():
    question = b"\x08opencode\x02ai\0\0\1\0\1"
    header = struct.pack("!6H", 42, 0x8180, 1, 1, 0, 0)
    answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4)
    public = header + question + answer + socket.inet_aton("1.1.1.1")
    assert direct.dns_answers(public, 42) == ("1.1.1.1",)
    for invalid in [header + question + answer + socket.inet_aton("198.18.0.2"), public[:-1], public]:
        with pytest.raises(ValueError):
            direct.dns_answers(invalid, 43 if invalid == public else 42)


def test_direct_tunnel_is_authenticated_host_scoped_opaque_and_closed(monkeypatch):
    monkeypatch.setattr(direct, "discover_interface", lambda *_: object())
    monkeypatch.setattr(direct, "resolve", lambda *_: ("1.1.1.1",))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    connected = []

    def echo():
        peer, _ = listener.accept()
        with peer:
            connected.append(True)
            while data := peer.recv(1024):
                peer.sendall(data)

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    monkeypatch.setattr(direct.DirectTunnel, "_connect", lambda self: socket.create_connection(listener.getsockname(), timeout=2))
    tunnel = direct.DirectTunnel("opencode.ai", time.monotonic() + 5, lambda: False)
    port = urlsplit(tunnel.proxy_url).port

    def connect(host, authorized):
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        authorization = tunnel._authorization if authorized else "Basic invalid"
        client.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\nProxy-Authorization: {authorization}\r\n\r\n".encode())
        header = b""
        while b"\r\n\r\n" not in header:
            header += client.recv(1)
        return client, header

    try:
        handler_class = tunnel.server.RequestHandlerClass
        handler = handler_class.__new__(handler_class)
        handler.log_message(format="ignored")
        for host, authorized in [("opencode.ai", False), ("unapproved.test", True)]:
            client, header = connect(host, authorized)
            with client:
                assert b" 403 " in header
        assert connected == []
        client, header = connect("opencode.ai", True)
        with client:
            assert b" 200 " in header
            opaque = b"\x16\x03\x03\x00\xffopaque-tls-bytes"
            client.sendall(opaque)
            assert client.recv(len(opaque)) == opaque
        idle_client = socket.create_connection(("127.0.0.1", port), timeout=2)
        idle_client.sendall(b"CONNECT opencode.ai:443 HTTP/1.1\r\n")
        deadline = time.monotonic() + 1
        while not tunnel._sockets and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        tunnel.close()
        listener.close()
        thread.join(2)
    assert not tunnel.thread.is_alive() and tunnel._sockets == set()
    with idle_client, contextlib.suppress(ConnectionResetError):
        assert idle_client.recv(1) == b""
