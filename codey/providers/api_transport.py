"""One bounded generation attempt. Unknown outcomes are never replayed."""
from __future__ import annotations

import base64
import contextlib
import datetime
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
from typing import Any, cast
from urllib.parse import unquote, urlsplit

from codey.providers.api_http import ConnectionKey
from codey.providers.api_request_policy import AttemptBudget
from codey.providers.diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_RATE_LIMITED,
    FAILURE_REQUEST_REJECTED,
    FAILURE_TRANSIENT,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_OPEN_WORK_SLOTS = threading.BoundedSemaphore(8)


class AttemptControl:
    """Cancellation handle shared by connect, response and body phases."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: list[Any] = []
        self._responses: list[Any] = []
        self.cancelled = False
        self.phase = "connecting"
        self.last_byte_at = time.monotonic()

    def register_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.append(connection)
            cancelled = self.cancelled
        if cancelled:
            self._shutdown(connection)

    def register_response(self, response: Any) -> None:
        with self._lock:
            self._responses.append(response)
            cancelled = self.cancelled
        if cancelled:
            abort_response(response)

    def enter_phase(self, phase: str) -> None:
        with self._lock:
            self.phase = phase

    def reset_body_timer(self) -> None:
        with self._lock:
            self.last_byte_at = time.monotonic()

    def note_body_bytes(self, data: bytes) -> None:
        if data:
            with self._lock:
                self.last_byte_at = time.monotonic()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            targets = [*self._connections, *self._responses]
        for target in targets:
            self._shutdown(target)

    def finish(self) -> None:
        with self._lock:
            self._connections.clear()
            self._responses.clear()

    @staticmethod
    def _shutdown(target: Any) -> None:
        sock = _socket_for(target)
        with contextlib.suppress(Exception):
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            target.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            target.close()


def _socket_for(target: Any) -> Any:
    """Find the live socket through HTTPError/HTTPResponse buffering layers."""
    candidates = [target, getattr(target, "fp", None), getattr(getattr(target, "fp", None), "fp", None),
                  getattr(target, "raw", None), getattr(getattr(target, "fp", None), "raw", None),
                  getattr(getattr(getattr(target, "fp", None), "fp", None), "raw", None)]
    for value in candidates:
        sock = getattr(value, "sock", None) or getattr(value, "_sock", None)
        if sock is not None:
            return sock
    return None


class BoundedBodyReader:
    def __init__(self, source: Any, *, control: AttemptControl | None = None,
                 deadline: float | None = None, max_bytes: int = MAX_RESPONSE_BYTES,
                 prefer_read1: bool = True, idle_timeout: float | None = None) -> None:
        self.source = source
        self.control = control or AttemptControl()
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.prefer_read1 = prefer_read1
        self.idle_timeout = idle_timeout
        self.total = 0
        self._line_buffer = bytearray()
        self._scan_position = 0

    def read_chunk(self, size: int = 65536) -> bytes:
        if self.control.cancelled:
            raise GenerationUnknownError("generation cancelled; late response discarded")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise GenerationUnknownError("generation deadline exceeded; not resent")
        if self.idle_timeout is not None and time.monotonic() - self.control.last_byte_at >= self.idle_timeout:
            raise GenerationUnknownError("generation body was silent; not resent")
        self._set_socket_timeout()
        reader = getattr(self.source, "read1", None) if self.prefer_read1 else None
        started = time.monotonic()
        try:
            data = reader(size) if callable(reader) else self.source.read(size)
        except TimeoutError as exc:
            if self.idle_timeout is not None and time.monotonic() - self.control.last_byte_at >= self.idle_timeout:
                raise GenerationUnknownError("generation body was silent; not resent") from exc
            raise GenerationUnknownError("generation deadline exceeded; not resent") from exc
        if not isinstance(data, bytes) and callable(reader):
            # Some deterministic response doubles expose every attribute as a
            # callable MagicMock; use their real ``read`` implementation.
            data = self.source.read(size)
        if self.idle_timeout is not None and time.monotonic() - started >= self.idle_timeout:
            raise GenerationUnknownError("generation body was silent; not resent")
        if not isinstance(data, bytes):
            raise GenerationUnknownError("generation response returned non-byte data")
        if data:
            self.total += len(data)
            self.control.note_body_bytes(data)
            if self.total > self.max_bytes:
                raise GenerationUnknownError("generation response exceeded its byte limit")
        return data

    def _set_socket_timeout(self) -> None:
        sock = None
        with contextlib.suppress(Exception):
            sock = _socket_for(self.source)
        if sock is None:
            return
        limits: list[float] = []
        now = time.monotonic()
        if self.deadline is not None:
            limits.append(max(0.001, self.deadline - now))
        if self.idle_timeout is not None:
            limits.append(max(0.001, self.idle_timeout - (now - self.control.last_byte_at)))
        if limits:
            with contextlib.suppress(OSError):
                sock.settimeout(min(limits))

    def readline(self, size: int = MAX_RESPONSE_BYTES + 1) -> bytes:
        while len(self._line_buffer) < size:
            marker = self._line_buffer.find(b"\n", self._scan_position)
            if marker >= 0:
                marker += 1
                result = bytes(self._line_buffer[:marker])
                del self._line_buffer[:marker]
                self._scan_position = 0
                return result
            self._scan_position = max(0, len(self._line_buffer) - 1)
            chunk = self.read_chunk(min(4096, size - len(self._line_buffer)))
            if not chunk:
                result = bytes(self._line_buffer)
                self._line_buffer.clear()
                self._scan_position = 0
                return result
            self._line_buffer.extend(chunk)
        raise GenerationUnknownError("generation stream line exceeded its byte limit")

    def read_json(self) -> dict[str, Any]:
        chunks: list[bytes] = []
        while True:
            chunk = self.read_chunk()
            if not chunk:
                break
            chunks.append(chunk)
        return _json_object(b"".join(chunks))


class GenerationUnknownError(RuntimeError):
    provider_failure_kind = "submission_uncertain"


class GenerationNotSentError(RuntimeError):
    provider_failure_kind = "not_submitted"


class GenerationRejectedError(RuntimeError):
    """A complete HTTP failure response, distinct from an unknown generation."""

    def __init__(self, status: int, detail: str, retry_after: str | None = None,
                 *, error_code: str | None = None, upstream_request_id: str | None = None) -> None:
        self.status = status
        self.retry_after: float | None = None
        if retry_after:
            if retry_after.isdecimal():
                self.retry_after = float(retry_after)
            else:
                with contextlib.suppress(ValueError, TypeError, OverflowError):
                    date = parsedate_to_datetime(retry_after)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=datetime.UTC)
                    self.retry_after = max(0.0, date.timestamp() - time.time())
        self.error_type = ""
        self.error_code = (error_code or "")[:80]
        self.upstream_request_id = (upstream_request_id or "")[:160]
        reason = detail
        with contextlib.suppress(ValueError, TypeError):
            body = json.loads(detail)
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                if isinstance(error.get("type"), str):
                    self.error_type = error["type"][:80]
                if isinstance(error.get("code"), str):
                    self.error_code = error["code"][:80]
                if isinstance(error.get("request_id"), str) and not self.upstream_request_id:
                    self.upstream_request_id = error["request_id"][:160]
                if isinstance(error.get("message"), str) and error["message"].strip():
                    reason = error["message"]
        label = f" [{self.error_type}]" if self.error_type else ""
        self.provider_failure_kind = (
            FAILURE_AUTHENTICATION_REQUIRED if status == 401 else
            FAILURE_RATE_LIMITED if status == 429 else
            FAILURE_TRANSIENT if 500 <= status <= 599 else
            FAILURE_REQUEST_REJECTED
        )
        self.provider_failure_facts: dict[str, object] = {"http_status": status}
        if self.error_type:
            self.provider_failure_facts["service_error_type"] = self.error_type
        if self.error_code:
            self.provider_failure_facts["service_error_code"] = self.error_code
        if self.upstream_request_id:
            self.provider_failure_facts["upstream_request_id"] = self.upstream_request_id
        super().__init__(f"model HTTP {status}{label}: {reason[:400]}")


class NoGenerationRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class _PooledResponse:
    """HTTPResponse facade that returns a clean connection only after EOF."""

    def __init__(self, response: Any, connection: Any, session: Any, key: ConnectionKey) -> None:
        self._response = response
        self._connection = connection
        self._session = session
        self._key = key
        self._closed = False
        self._read = 0
        self.status = getattr(response, "status", getattr(response, "code", 0))
        self.code = self.status
        self.reason = getattr(response, "reason", "")
        self.headers = response.headers
        self.msg = getattr(response, "msg", self.headers)

    def __enter__(self) -> _PooledResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def read(self, size: int = -1) -> bytes:
        data = cast(bytes, self._response.read(size))
        self._read += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        reader = getattr(self._response, "read1", None)
        data = cast(bytes, reader(size) if callable(reader) else self._response.read(size))
        self._read += len(data)
        return data

    def readline(self, size: int = -1) -> bytes:
        data = cast(bytes, self._response.readline(size))
        self._read += len(data)
        return data

    def _framing_complete(self) -> bool:
        if str(self.headers.get("Connection", "")).lower() == "close":
            return False
        length = self.headers.get("Content-Length")
        if length is not None:
            with contextlib.suppress(ValueError):
                return self._read >= int(length)
        return bool(getattr(self._response, "isclosed", lambda: False)())

    @property
    def sock(self) -> Any:
        return (getattr(self._connection, "sock", None)
                or getattr(self._response, "sock", None)
                or _socket_for(self._response))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        complete = self._framing_complete()
        with contextlib.suppress(Exception):
            self._response.close()
        if complete:
            self._session.release_connection(self._key, self._connection)
        else:
            self._session.discard_connection(self._connection)


def _open_pooled_request(request: urllib.request.Request, timeout: float, *, control: AttemptControl,  # noqa: C901, PLR0912, PLR0915
                         http_session: Any, transport_options: Mapping[str, object] | None = None) -> Any:  # noqa: C901
    """Send one request through a caller-owned keep-alive pool.

    This path deliberately has no POST retry.  The caller's AttemptBudget and
    submission state decide whether another logical attempt is safe.
    """
    parts = urlsplit(request.full_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"unsupported pooled URL: {request.full_url}")
    options = transport_options or {}
    proxy = str(options.get("proxy") or "")
    if not proxy:
        proxies = urllib.request.getproxies()
        candidate = proxies.get(parts.scheme) or proxies.get("all") or ""
        if candidate and not urllib.request.proxy_bypass(parts.hostname):
            proxy = str(candidate)
    identity = str(options.get("identity") or "")
    tls = str(options.get("tls") or parts.scheme)
    key = ConnectionKey(parts.scheme + "://" + parts.netloc, proxy, identity, tls)
    connection = http_session.acquire_connection(key)
    if connection is None:
        if proxy:
            proxy_parts = urlsplit(proxy)
            if not proxy_parts.hostname:
                raise ValueError("proxy URL must include a host")
            connection_host = proxy_parts.hostname
            connection_port = proxy_parts.port
        else:
            connection_host = parts.hostname
            connection_port = parts.port
        if parts.scheme == "https":
            context = ssl.create_default_context()
            proxy_auth = _proxy_authorization(proxy)
            class PooledHTTPSConnection(http.client.HTTPSConnection):
                def connect(self) -> None:
                    active = getattr(self, "_codey_control", control)
                    if bool(getattr(active, "cancelled", False)):
                        raise GenerationNotSentError("generation cancelled before connection")
                    super().connect()
                    if bool(getattr(active, "cancelled", False)):
                        self.close()
                        raise GenerationNotSentError("generation cancelled before submission")

            connection = PooledHTTPSConnection(connection_host, connection_port, timeout=timeout,
                                               context=context)
            if proxy:
                tunnel_headers = {"Proxy-Authorization": proxy_auth} if proxy_auth else {}
                connection.set_tunnel(parts.hostname, parts.port or 443, headers=tunnel_headers)
        else:
            class PooledHTTPConnection(http.client.HTTPConnection):
                def connect(self) -> None:
                    active = getattr(self, "_codey_control", control)
                    if bool(getattr(active, "cancelled", False)):
                        raise GenerationNotSentError("generation cancelled before connection")
                    super().connect()
                    if bool(getattr(active, "cancelled", False)):
                        self.close()
                        raise GenerationNotSentError("generation cancelled before submission")

            connection = PooledHTTPConnection(connection_host, connection_port, timeout=timeout)
    else:
        connection.timeout = timeout
    connection._codey_control = control
    if getattr(connection, "sock", None) is not None:
        with contextlib.suppress(OSError):
            connection.sock.settimeout(timeout)
    control.register_connection(connection)
    if control.cancelled:
        http_session.discard_connection(connection)
        raise GenerationNotSentError("generation cancelled before submission")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = dict(request.header_items())
    headers.setdefault("Connection", "keep-alive")
    proxy_auth = _proxy_authorization(proxy)
    if proxy and parts.scheme == "http" and proxy_auth:
        headers["Proxy-Authorization"] = proxy_auth
    body = request.data
    target = request.full_url if proxy and parts.scheme == "http" else path
    # Once request() starts, a server may have received bytes.  Cancellation
    # after this point is therefore an unknown outcome.
    try:
        connection.request(request.method or "GET", target, body=body, headers=headers)
    except (OSError, http.client.HTTPException) as exc:
        if bool(getattr(control, "cancelled", False)):
            http_session.discard_connection(connection)
            raise GenerationUnknownError("generation cancelled after submission started") from exc
        http_session.discard_connection(connection)
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            raise GenerationNotSentError(f"generation request was not submitted: {reason}") from exc
        raise
    if bool(getattr(control, "cancelled", False)):
        http_session.discard_connection(connection)
        raise GenerationUnknownError("generation cancelled after submission started")
    try:
        response = connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        http_session.discard_connection(connection)
        if control.cancelled:
            raise GenerationUnknownError("generation cancelled while waiting for response headers") from exc
        raise
    wrapped = _PooledResponse(response, connection, http_session, key)
    control.register_response(wrapped)
    if wrapped.status >= 400:
        raise urllib.error.HTTPError(request.full_url, wrapped.status, wrapped.reason, wrapped.headers, cast(Any, wrapped))
    return wrapped


def _proxy_authorization(proxy: str) -> str | None:
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.username is None:
        return None
    credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()
    return "Basic " + base64.b64encode(credentials).decode("ascii")


class _SubmissionHTTPSHandler(urllib.request.HTTPSHandler):
    """Track generation bytes independently of TLS and proxy CONNECT bytes."""

    def __init__(self, submission: dict[str, bool], control: AttemptControl | None = None) -> None:
        self.context = ssl.create_default_context()
        self.context.set_alpn_protocols(["http/1.1"])
        super().__init__(context=self.context)
        self.submission = submission
        self.control = control

    def https_open(self, request: Any) -> Any:
        submission = self.submission
        tracked_control = self.control

        class Connection(http.client.HTTPSConnection):
            establishing = False

            def send(self, data: Any) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    if submission.get("attempted"):
                        raise GenerationUnknownError("generation cancelled after submission started")
                    raise GenerationNotSentError("generation cancelled before submission")
                if self.sock is None and self.auto_open:
                    self.establishing = True
                    try:
                        self.connect()
                    finally:
                        self.establishing = False
                if not self.establishing:
                    # Set before sendall: a failed write may have sent bytes.
                    if tracked_control is not None and tracked_control.cancelled:
                        if submission.get("attempted"):
                            raise GenerationUnknownError("generation cancelled after submission started")
                        raise GenerationNotSentError("generation cancelled before submission")
                    submission["attempted"] = True
                super().send(data)

            def connect(self) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before connection")
                if tracked_control is not None:
                    tracked_control.register_connection(self)
                super().connect()
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before submission")

        submission["attempted"] = False
        return self.do_open(Connection, request, context=self.context)


class _SubmissionHTTPHandler(urllib.request.HTTPHandler):
    """Track plain HTTP sockets as well as TLS sockets."""

    def __init__(self, submission: dict[str, bool], control: AttemptControl | None = None) -> None:
        super().__init__()
        self.submission = submission
        self.control = control

    def http_open(self, request: Any) -> Any:
        submission = self.submission
        tracked_control = self.control

        class Connection(http.client.HTTPConnection):
            establishing = False

            def connect(self) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before connection")
                if tracked_control is not None:
                    tracked_control.register_connection(self)
                super().connect()
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before submission")

            def send(self, data: Any) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    if submission.get("attempted"):
                        raise GenerationUnknownError("generation cancelled after submission started")
                    raise GenerationNotSentError("generation cancelled before submission")
                if self.sock is None and self.auto_open:
                    self.establishing = True
                    try:
                        self.connect()
                    finally:
                        self.establishing = False
                if not self.establishing:
                    if tracked_control is not None and tracked_control.cancelled:
                        if submission.get("attempted"):
                            raise GenerationUnknownError("generation cancelled after submission started")
                        raise GenerationNotSentError("generation cancelled before submission")
                    submission["attempted"] = True
                super().send(data)

        submission["attempted"] = False
        return self.do_open(Connection, request)


def _open_request_sync(request: urllib.request.Request, timeout: float, *, control: AttemptControl | None = None,
                       attempt_budget: AttemptBudget | None = None,
                       http_session: Any = None, transport_options: Mapping[str, object] | None = None) -> Any:
    if http_session is not None:
        active_control = control or AttemptControl()
        return _open_pooled_request(request, timeout, control=active_control,
                                    http_session=http_session, transport_options=transport_options)
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        submission: dict[str, bool] = {}
        opener = urllib.request.build_opener(NoGenerationRedirect(), _SubmissionHTTPHandler(submission, control),
                                             _SubmissionHTTPSHandler(submission, control))
        try:
            return opener.open(request, timeout=max(0.001, deadline - time.monotonic()))
        except urllib.error.URLError as exc:
            if submission.get("attempted") is not False or not isinstance(exc.reason, ssl.SSLError):
                raise
            # TLS failed before HTTP generation headers/body. Reconnect once
            # for EOF only; certificate failures and exhausted budget stop.
            if (isinstance(exc.reason, ssl.SSLEOFError) and attempt == 0 and time.monotonic() < deadline
                    and (attempt_budget is None or attempt_budget.reserve("tls_reconnect"))):
                continue
            raise GenerationNotSentError(f"TLS connection failed before generation submission: {exc.reason}") from exc
    raise AssertionError("unreachable connection retry state")


def open_request(request: urllib.request.Request, timeout: float, *, control: AttemptControl | None = None,
                 attempt_budget: AttemptBudget | None = None,
                 http_session: Any = None, transport_options: Mapping[str, object] | None = None) -> Any:
    """Open one request while allowing Stop to leave a blocked header read."""
    if control is None:
        return _open_request_sync(request, timeout, attempt_budget=attempt_budget,
                                  http_session=http_session, transport_options=transport_options)
    import queue

    result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    slot_deadline = time.monotonic() + timeout
    while not _OPEN_WORK_SLOTS.acquire(timeout=0.02):
        if control.cancelled:
            raise GenerationUnknownError("generation cancelled while waiting for open slot")
        if time.monotonic() >= slot_deadline:
            raise GenerationUnknownError("generation deadline exceeded while waiting for open slot")

    def worker() -> None:
        try:
            result.put((True, _open_request_sync(request, timeout, control=control,
                                                 attempt_budget=attempt_budget, http_session=http_session,
                                                 transport_options=transport_options)))
        except BaseException as exc:
            with contextlib.suppress(queue.Full):
                result.put((False, exc))
        finally:
            _OPEN_WORK_SLOTS.release()

    thread = threading.Thread(target=worker, daemon=True, name="codey-api-open")
    thread.start()
    deadline = time.monotonic() + timeout
    while True:
        try:
            ok, value = result.get(timeout=0.02)
        except queue.Empty:
            if control.cancelled:
                control.cancel()
                raise GenerationUnknownError("generation cancelled while opening request") from None
            if time.monotonic() >= deadline:
                control.cancel()
                raise GenerationUnknownError("generation deadline exceeded while opening request") from None
            continue
        if ok:
            if control.cancelled:
                abort_response(value)
                raise GenerationUnknownError("generation cancelled while opening request")
            return value
        raise value


def abort_response(response: Any) -> None:
    if response is None:
        return
    # Shutdown first so close does not wait for a blocked buffered read.
    with contextlib.suppress(AttributeError, OSError):
        sock = _socket_for(response)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(Exception):
        response.close()


def _json_object(raw: bytes) -> dict[str, Any]:
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GenerationUnknownError("generation response was incomplete or invalid JSON; not resent") from exc
    if not isinstance(body, dict):
        raise GenerationUnknownError("generation response was not an object; not resent")
    return body


def read_sse(response: Any, *, deadline: float, cancelled: Callable[[], bool],
             on_event: Callable[[dict[str, Any]], None] = lambda event: None,
             control: AttemptControl | None = None,
             idle_timeout: float | None = None) -> dict[str, Any]:
    data: list[bytes] = []
    total = 0
    chat: dict[str, Any] = {"content": "", "tool_calls": []}
    calls: dict[int, dict[str, Any]] = {}
    chat_finish = ""
    chat_done = False
    reader = BoundedBodyReader(response, control=control, deadline=deadline, idle_timeout=idle_timeout)
    while True:
        if cancelled():
            raise GenerationUnknownError("generation cancelled; late response discarded")
        if time.monotonic() >= deadline:
            raise GenerationUnknownError("generation deadline exceeded; not resent")
        line = reader.readline(MAX_RESPONSE_BYTES + 1)
        total += len(line)
        if total > MAX_RESPONSE_BYTES:
            raise GenerationUnknownError("generation stream exceeded its byte limit")
        if not line:
            break
        if line.strip():
            if line.startswith(b"data:"):
                data.append(line[5:].strip())
            continue
        if not data:
            continue
        frame = b"\n".join(data)
        data = []
        if frame == b"[DONE]":
            chat_done = True
            break
        event = _json_object(frame)
        on_event(event)
        kind = event.get("type")
        if kind in {"response.completed", "response.incomplete", "response.failed"}:
            result = event.get("response")
            if not isinstance(result, dict):
                raise GenerationUnknownError("Responses stream ended without a response object")
            return result
        if kind == "error" or "error" in event:
            raise RuntimeError(f"generation stream error: {str(event.get('error', event))[:400]}")
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            chat["content"] += delta.get("content") or ""
            for name in ["reasoning_content", "reasoning"]:
                if delta.get(name):
                    chat[name] = chat.get(name, "") + delta[name]
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call["index"], {"id": "", "type": "function",
                                                       "function": {"name": "", "arguments": ""}})
                slot["id"] += call.get("id") or ""
                for key in ["name", "arguments"]:
                    slot["function"][key] += (call.get("function") or {}).get(key) or ""
            chat_finish = choice.get("finish_reason") or chat_finish
    if chat_finish and chat_done:
        chat["tool_calls"] = [calls[key] for key in sorted(calls)]
        return {"choices": [{"message": chat, "finish_reason": chat_finish}]}
    raise GenerationUnknownError("generation stream ended without a valid terminal event; not resent")


def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], *, timeout: float,  # noqa: PLR0912, PLR0915
             observe: Callable[..., None], cancelled: Callable[[], bool] = lambda: False,
             opened: Callable[[Any], None] = lambda response: None,
             on_event: Callable[[dict[str, Any]], None] = lambda event: None,
             on_control: Callable[[AttemptControl], None] = lambda control: None,
             attempt_budget: AttemptBudget | None = None,
             idle_timeout: float | None = None,
             http_session: Any = None,
             transport_options: Mapping[str, object] | None = None) -> dict[str, Any]:
    from codey.providers import error_classification as errors

    if cancelled():
        raise GenerationNotSentError("generation cancelled before submission")
    data = json.dumps(payload, ensure_ascii=False).encode("utf8")
    request = urllib.request.Request(endpoint, data=data, headers=dict(headers), method="POST")
    started = time.monotonic()
    phase, count = "error", 0
    observe(attempt=1, data=data, phase="request", response_bytes=0, seconds=0.0)
    control = AttemptControl()
    on_control(control)
    deadline_guard = threading.Timer(max(0.001, started + timeout - time.monotonic()), control.cancel)
    deadline_guard.daemon = True
    deadline_guard.start()
    try:
        opener = open_request
        # Keep the small transport seam usable by deterministic test transports
        # that implement the historical two-argument callable.
        import inspect
        parameters = inspect.signature(opener).parameters
        if "control" in parameters:
            kwargs: dict[str, object] = {"control": control}
            if "attempt_budget" in parameters:
                kwargs["attempt_budget"] = attempt_budget
            if "http_session" in parameters:
                kwargs["http_session"] = http_session
            if "transport_options" in parameters:
                kwargs["transport_options"] = {
                    **(dict(transport_options) if transport_options is not None else {}),
                    "body_idle_timeout": idle_timeout,
                }
            response_context = cast(Any, opener)(request, timeout, **kwargs)
        else:
            response_context = opener(request, timeout)
        with response_context as response:
            control.register_response(response)
            control.enter_phase("response_headers")
            opened(response)
            # A slow partial JSON body or SSE line can continuously reset the
            # socket timeout. Close the socket at the total deadline as well.
            if "text/event-stream" in str(getattr(response, "headers", {}).get("Content-Type", "")):
                control.enter_phase("body")
                control.reset_body_timer()
                body = read_sse(response, deadline=started + timeout, cancelled=cancelled,
                                on_event=on_event, control=control, idle_timeout=idle_timeout)
            else:
                control.enter_phase("body")
                control.reset_body_timer()
                reader = BoundedBodyReader(response, control=control, deadline=started + timeout,
                                           idle_timeout=idle_timeout)
                # JSON responses are framed by the HTTP content length (or
                # EOF); one bounded read preserves that framing while the
                # reader still records the actual bytes received.
                read1 = getattr(response, "read1", None)
                has_streaming_read = callable(read1) and "unittest.mock" not in type(read1).__module__
                if has_streaming_read:
                    chunks: list[bytes] = []
                    while True:
                        chunk = reader.read_chunk(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                else:
                    raw = reader.read_chunk(MAX_RESPONSE_BYTES + 1)
                count = len(raw)
                if count > MAX_RESPONSE_BYTES:
                    raise GenerationUnknownError("generation response exceeded its byte limit")
                body = _json_object(raw)
                on_event(body)
        if cancelled():
            raise GenerationUnknownError("generation cancelled; late response discarded")
        if time.monotonic() >= started + timeout:
            raise GenerationUnknownError("generation deadline exceeded; late response discarded")
        phase = "response"
        return body
    except GenerationUnknownError:
        phase = "unknown"
        raise
    except GenerationNotSentError:
        phase = "not_sent"
        raise
    except urllib.error.HTTPError as exc:
        control.register_response(exc)
        if control.cancelled or cancelled():
            raise GenerationUnknownError("generation cancelled while reading rejection") from exc
        control.reset_body_timer()
        with contextlib.closing(exc):
            error_reader = BoundedBodyReader(exc, control=control, deadline=started + timeout, max_bytes=2000,
                                             prefer_read1=False, idle_timeout=idle_timeout)
            detail = error_reader.read_chunk(2000).decode("utf8", "replace")
        if errors.classify_http_error(exc.code, detail) == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError(detail[:400]) from exc
        raise GenerationRejectedError(
            exc.code,
            detail,
            exc.headers.get("Retry-After"),
            upstream_request_id=exc.headers.get("X-Request-Id") or exc.headers.get("X-Request-ID"),
        ) from exc
    except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            phase = "not_sent"
            raise GenerationNotSentError(f"generation request was not submitted: {reason}") from exc
        phase = "unknown"
        raise GenerationUnknownError(f"generation outcome unknown; request not resent: {exc}") from exc
    finally:
        deadline_guard.cancel()
        control.finish()
        opened(None)
        observe(attempt=1, data=data, phase=phase, response_bytes=count, seconds=time.monotonic() - started)
