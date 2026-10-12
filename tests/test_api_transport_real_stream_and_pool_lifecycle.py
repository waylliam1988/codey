"""Real sockets verify early cancellation, byte activity and HTTP reuse."""
import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from codey.providers.api_http import ApiHttpSession, ConnectionKey
from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationNotSentError, GenerationRejectedError, GenerationUnknownError

CHAT = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


@contextlib.contextmanager
def service(mode="json"):
    received, release = threading.Event(), threading.Event()
    requests, clients = [], []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_):
            pass
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            clients.append(self.client_address)
            received.set()
            if mode == "headers":
                release.wait(3)
                return
            if mode == "error_silent":
                self.send_response(429)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.flush()
                release.wait(3)
                return
            if mode == "slow_headers":
                time.sleep(0.12)
            if mode == "slow_error":
                time.sleep(0.12)
                body = b'{"error":{"type":"RateLimitError","code":"rate_limit"}}'
                self.send_response(429)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.flush()
                self.wfile.write(body)
                self.wfile.flush()
                return
            if mode == "json":
                body = json.dumps(CHAT).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                return
            if mode == "close":
                body = json.dumps(CHAT).encode()
                self.send_response(200)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            data = (b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\r\n\r\n'
                    b'data: [DONE]\r\n\r\n')
            try:
                parts = [data] if mode == "terminal_open" else [data[i:i+3] for i in range(0, len(data), 3)]
                for part in parts:
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    self.wfile.flush()
                    if mode != "terminal_open":
                        time.sleep(0.005)
                if mode == "terminal_open":
                    release.wait(3)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, clients, received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("mode", ["headers", "error_silent"])
def test_stop_interrupts_real_socket_before_headers_or_during_error_body(mode):
    with service(mode) as (url, requests, _, received):
        provider = ApiProvider(url, "m", timeout=5)
        errors = []
        def run():
            try:
                provider.send("hello")
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        assert received.wait(1)
        started = time.monotonic()
        provider.abandon_inflight()
        thread.join(1)
        assert not thread.is_alive(), "Stop must shutdown the actual socket, not wait for the deadline"
        assert time.monotonic() - started < 1
        assert errors and isinstance(errors[0], (GenerationNotSentError, GenerationUnknownError))
        assert len(requests) == 1


def test_two_completed_requests_reuse_real_connection():
    with service() as (url, requests, clients, _):
        session = ApiHttpSession()
        provider = ApiProvider(url, "m", http_session=session)
        try:
            assert provider.send("one") == provider.send("two") == "ok"
            assert len(requests) == 2
            assert clients[0] == clients[1]
        finally:
            provider.close()
            session.release()


def test_silent_error_body_honors_byte_idle_timeout_without_stop():
    with service("error_silent") as (url, requests, _, received):
        provider = ApiProvider(url, "m", timeout=2, transport_options={"body_idle_timeout": 0.1})
        started = time.monotonic()
        try:
            with pytest.raises(GenerationUnknownError):
                provider.send("hello")
            assert received.is_set()
            assert time.monotonic() - started < 1
            assert len(requests) == 1
        finally:
            provider.close()


def test_pooled_stop_during_connect_never_sends_post(monkeypatch):
    with service("json") as (url, requests, _, _):
        provider = ApiProvider(url, "m", timeout=2, http_session=ApiHttpSession(),
                               transport_options={"body_idle_timeout": 0.1})
        entered, release = threading.Event(), threading.Event()
        import http.client
        original = http.client.HTTPConnection.connect

        def blocked_connect(connection):
            entered.set()
            release.wait(1)
            return original(connection)

        monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked_connect)
        errors = []
        thread = threading.Thread(target=lambda: _send_capturing(provider, errors))
        thread.start()
        assert entered.wait(1)
        provider.abandon_inflight()
        release.set()
        thread.join(1)
        assert not thread.is_alive()
        assert errors and isinstance(errors[0], (GenerationNotSentError, GenerationUnknownError))
        assert requests == []
        provider.close()


def _send_capturing(provider, errors):
    try:
        provider.send("hello")
    except Exception as exc:  # noqa: BLE001
        errors.append(exc)


def test_pooled_silent_body_uses_socket_idle_timeout():
    with service("error_silent") as (url, requests, _, received):
        provider = ApiProvider(url, "m", timeout=2, http_session=ApiHttpSession(),
                               transport_options={"body_idle_timeout": 0.1})
        started = time.monotonic()
        try:
            with pytest.raises(GenerationUnknownError):
                provider.send("hello")
            assert received.is_set() and len(requests) == 1
            assert time.monotonic() - started < 1
        finally:
            provider.close()


def test_response_header_time_does_not_count_as_body_silence():
    with service("slow_headers") as (url, requests, _, received):
        provider = ApiProvider(url, "m", timeout=2, http_session=ApiHttpSession(),
                               transport_options={"body_idle_timeout": 0.05})
        try:
            assert provider.send("hello") == "ok"
            assert len(requests) == 1 and received.is_set()
        finally:
            provider.close()


def test_slow_error_headers_reset_body_silence_timer():
    with service("slow_error") as (url, requests, _, received):
        provider = ApiProvider(url, "m", timeout=2, transport_options={"body_idle_timeout": 0.05})
        try:
            with pytest.raises(GenerationRejectedError) as caught:
                provider.send("hello")
            assert "RateLimitError" in str(caught.value)
            assert received.is_set() and len(requests) == 1
        finally:
            provider.close()


def test_connection_close_response_is_not_returned_to_pool():
    with service("close") as (url, requests, clients, _):
        session = ApiHttpSession()
        provider = ApiProvider(url, "m", timeout=2, http_session=session)
        try:
            assert provider.send("one") == provider.send("two") == "ok"
            assert len(requests) == 2 and clients[0] != clients[1]
        finally:
            provider.close()
            session.release()


def test_reused_connection_rebinds_stop_control_before_reconnect(monkeypatch):
    with service() as (url, requests, _, _):
        session = ApiHttpSession()
        provider = ApiProvider(url, "m", timeout=2, http_session=session)
        assert provider.send("one") == "ok"
        key = ConnectionKey(url, "", "", "http")
        connection = session.acquire_connection(key)
        assert connection is not None
        connection.close()
        session.release_connection(key, connection)
        import http.client
        entered, release = threading.Event(), threading.Event()
        original = http.client.HTTPConnection.connect
        def blocked(connection):
            entered.set()
            release.wait(1)
            return original(connection)
        monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked)
        errors = []
        thread = threading.Thread(target=lambda: _send_capturing(provider, errors))
        thread.start()
        assert entered.wait(1)
        provider.abandon_inflight()
        release.set()
        thread.join(1)
        assert not thread.is_alive() and errors
        assert len(requests) == 1
        provider.close()
        session.release()


def test_pool_uses_environment_proxy_when_not_bypassed(monkeypatch):
    with service("json") as (url, requests, _, _):
        provider = ApiProvider(url, "m", timeout=2, http_session=ApiHttpSession())
        import http.client
        monkeypatch.setattr(http.client.HTTPConnection, "connect",
                            lambda _connection: (_ for _ in ()).throw(ConnectionRefusedError("fixture")))
        with patch("urllib.request.getproxies", return_value={"http": "http://127.0.0.1:9"}), \
             patch("urllib.request.proxy_bypass", return_value=False), \
             pytest.raises(GenerationNotSentError):
            provider.send("hello")
        assert requests == []
        provider.close()


def test_http_proxy_forwards_absolute_target_and_proxy_credentials():
    with service("json") as (url, requests, _, _):
        seen = []
        class ProxyHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                seen.append((self.path, self.headers.get("Proxy-Authorization")))
                body = self.rfile.read(int(self.headers["Content-Length"]))
                opener = __import__("urllib.request", fromlist=["build_opener"]).build_opener(
                    __import__("urllib.request", fromlist=["ProxyHandler"]).ProxyHandler({}))
                response = opener.open(__import__("urllib.request", fromlist=["Request"]).Request(
                    self.path, data=body, method="POST"), timeout=2)
                result = response.read()
                self.send_response(response.status)
                self.send_header("Content-Length", str(len(result)))
                self.end_headers()
                self.wfile.write(result)
        proxy_server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        proxy_thread.start()
        try:
            session = ApiHttpSession()
            provider = ApiProvider(url, "m", timeout=2, http_session=session,
                                   transport_options={"proxy": f"http://user:pass@127.0.0.1:{proxy_server.server_port}"})
            try:
                assert provider.send("hello") == "ok"
            finally:
                provider.close()
                session.release()
            assert seen and seen[0][0] == url + "/chat/completions"
            assert seen[0][1] == "Basic dXNlcjpwYXNz"
            assert len(requests) == 1
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(2)


def test_last_session_release_closes_idle_connections():
    class Connection:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True
    session = ApiHttpSession()
    connection = Connection()
    from codey.providers.api_http import ConnectionKey
    key = ConnectionKey("http://example", "", "", "http")
    session.release_connection(key, connection)
    session.release()
    assert connection.closed is True


@pytest.mark.parametrize("mode", ["slow", "terminal_open"])
def test_short_sse_and_slow_half_lines_return_without_waiting_for_framing(mode):
    with service(mode) as (url, requests, _, _):
        provider = ApiProvider(url, "m", stream=True, timeout=2,
                               http_session=ApiHttpSession(),
                               transport_options={"body_idle_timeout": 0.1})
        started = time.monotonic()
        try:
            assert provider.send("hello") == "ok"
            assert time.monotonic() - started < 1.5
            assert len(requests) == 1
        finally:
            provider.close()
