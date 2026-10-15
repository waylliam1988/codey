"""One bounded generation attempt. Unknown outcomes are never replayed."""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
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

from codey.providers.api_http import ConnectionKey, ConnectionLease
from codey.providers.api_request_policy import AttemptBudget
from codey.providers.api_stream import read_sse as read_sse
from codey.providers.diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_RATE_LIMITED,
    FAILURE_REQUEST_REJECTED,
    FAILURE_TRANSIENT,
)
from codey.providers.error_classification import RequestPrepError

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
_OPEN_WORK_SLOTS = threading.BoundedSemaphore(8)


class AttemptControl:
    """Cancellation handle shared by connect, response and body phases."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: list[Any] = []
        self._leases: list[ConnectionLease] = []
        self._responses: list[Any] = []
        self.cancelled = False
        self.submission_started = False
        self.submission_possible = False
        self.phase = "connecting"
        self.last_byte_at = time.monotonic()

    def register_connection(self, connection: Any) -> None:
        with self._lock:
            self._connections.append(connection)
            cancelled = self.cancelled
        if cancelled:
            self._shutdown(connection)

    def register_lease(self, lease: ConnectionLease) -> None:
        with self._lock:
            self._leases.append(lease)
            cancelled = self.cancelled
        if cancelled:
            connection = lease.cancel()
            if connection is not None:
                self._shutdown(connection)

    def register_response(self, response: Any) -> None:
        with self._lock:
            self._responses.append(response)
            cancelled = self.cancelled
        if cancelled:
            abort_response(response)

    def mark_submission_started(self) -> None:
        with self._lock:
            self.submission_started = True

    def mark_submission_possible(self) -> None:
        """An asynchronous peer may write before its receipt reaches us."""
        with self._lock:
            self.submission_possible = True

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
            leases = list(self._leases)
        for lease in leases:
            connection = lease.cancel()
            if connection is not None:
                self._shutdown(connection)
        for target in targets:
            if not isinstance(target, _PooledResponse):
                self._shutdown(target)

    def finish(self) -> None:
        with self._lock:
            self._connections.clear()
            self._leases.clear()
            self._responses.clear()

    @staticmethod
    def _shutdown(target: Any) -> None:
        abort = getattr(target, "abort", None)
        if callable(abort):
            with contextlib.suppress(Exception):
                abort()
            return
        sock = _socket_for(target)
        with contextlib.suppress(Exception):
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            target.shutdown(socket.SHUT_RDWR)
        if isinstance(target, (http.client.HTTPResponse, urllib.error.HTTPError)):
            return
        if (isinstance(target, http.client.HTTPConnection)
                and getattr(target, "sock", None) is None
                and getattr(target, "_tracked_socket", None) is None):
            return
        with contextlib.suppress(Exception):
            target.close()


def _socket_for(target: Any) -> Any:
    """Find the live socket through HTTPError/HTTPResponse buffering layers."""
    candidates = [target, getattr(target, "_tracked_socket", None), getattr(target, "fp", None),
                  getattr(getattr(target, "fp", None), "fp", None),
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

class GenerationUnknownError(RuntimeError):
    provider_failure_kind = "submission_uncertain"

    def __init__(self, message: str, *, reason_code: str = "submission_uncertain") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class GenerationNotSentError(RuntimeError):
    provider_failure_kind = "not_submitted"

    def __init__(self, message: str, *, reason_code: str = "not_submitted") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class GenerationRejectedError(RuntimeError):
    """A complete HTTP failure response, distinct from an unknown generation."""

    error_type: str

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
        self.error_type: str = ""
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
        label = f" [{self.error_type}]" if self.error_type != "" else ""
        self.service_message = reason[:400]
        self.provider_failure_kind = (
            FAILURE_AUTHENTICATION_REQUIRED if status == 401 else
            FAILURE_RATE_LIMITED if status == 429 else
            FAILURE_TRANSIENT if 500 <= status <= 599 else
            FAILURE_REQUEST_REJECTED
        )
        self.provider_failure_facts: dict[str, object] = {"http_status": status}
        if self.error_type != "":
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

    def __init__(self, response: Any, lease: ConnectionLease) -> None:
        self._response = response
        self._lease = lease
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
        connection = self._lease.connection
        return (getattr(connection, "sock", None)
                or getattr(self._response, "sock", None)
                or _socket_for(self._response))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        complete = self._framing_complete()
        close_ok = True
        try:
            self._response.close()
        except Exception:
            close_ok = False
        connection = self._lease.begin_return()
        if connection is None:
            return
        self._lease.finish_return(connection, complete and close_ok)

    def abort(self) -> None:
        connection = self._lease.cancel()
        if connection is not None:
            AttemptControl._shutdown(connection)
            with contextlib.suppress(Exception):
                self._response.close()
            self._lease.finish_cancel(connection)


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
            proxy = candidate
    if proxy:
        proxy_scheme = urlsplit(proxy).scheme.lower()
        if proxy_scheme != "http":
            raise RequestPrepError(f"unsupported proxy scheme: {proxy_scheme or 'unknown'}")
    identity = str(options.get("identity") or "")
    tls = str(options.get("tls") or parts.scheme)
    key = ConnectionKey(parts.scheme + "://" + parts.netloc, _proxy_pool_identity(proxy), identity, tls)
    lease = http_session.acquire_lease(key)
    if lease is None:
        lease = ConnectionLease(http_session, key)
    control.register_lease(lease)
    connection: Any = lease.connection
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
                    self.establishing = True
                    try:
                        super().connect()
                    finally:
                        self.establishing = False
                    if bool(getattr(active, "cancelled", False)):
                        self.close()
                        raise GenerationNotSentError("generation cancelled before submission")

                def send(self, data: Any) -> None:
                    if self.sock is None:
                        self.establishing = True
                        try:
                            self.connect()
                        finally:
                            self.establishing = False
                    if not getattr(self, "establishing", False):
                        active = getattr(self, "_codey_control", control)
                        if active.cancelled:
                            raise GenerationNotSentError("generation cancelled before submission")
                        self._codey_submission_started = True
                        active.mark_submission_started()
                    http.client.HTTPConnection.send(self, data)

            connection = PooledHTTPSConnection(connection_host, connection_port, timeout=timeout,
                                               context=context)
            if proxy:
                tunnel_headers = {"Proxy-Authorization": proxy_auth} if proxy_auth else {}
                connection.set_tunnel(parts.hostname, parts.port or 443, headers=tunnel_headers)
        else:
            class PooledHTTPConnection(http.client.HTTPConnection):
                def send(self, data: Any) -> None:
                    if self.sock is None:
                        self.connect()
                    active = getattr(self, "_codey_control", control)
                    if active.cancelled:
                        raise GenerationNotSentError("generation cancelled before submission")
                    self._codey_submission_started = True
                    active.mark_submission_started()
                    super().send(data)

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
    attached: Any = lease.connection
    if attached is None:
        if not lease.attach(connection):
            http_session.discard_connection(connection)
            raise GenerationNotSentError("generation connection ownership was not established")
        attached = lease.connection
    if attached is None:
        raise GenerationNotSentError("generation connection ownership was not established")
    active_connection: Any = attached
    active_connection._codey_submission_started = False
    setattr(active_connection, "_codey_control", control)  # noqa: B010
    if getattr(active_connection, "sock", None) is not None:
        with contextlib.suppress(OSError):
            active_connection.sock.settimeout(timeout)
    def cancel_lease() -> None:
        owned = lease.cancel()
        if owned is None:
            return
        AttemptControl._shutdown(owned)
        with contextlib.suppress(Exception):
            lease.finish_cancel(owned)

    if control.cancelled:
        cancel_lease()
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
    # Once request() starts, cancellation may race a server that received bytes; outcome is unknown.
    try:
        active_connection.request(request.method or "GET", target, body=body, headers=headers)
    except (OSError, http.client.HTTPException) as exc:
        if bool(getattr(control, "cancelled", False)):
            cancel_lease()
            raise GenerationUnknownError("generation cancelled after submission started") from exc
        cancel_lease()
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLEOFError):
            if bool(getattr(active_connection, "_codey_submission_started", False)):
                raise GenerationUnknownError("TLS connection closed after generation write",
                                             reason_code="post_submission_tls_eof") from exc
            raise GenerationNotSentError("TLS connection closed before generation submission",
                                         reason_code="pre_submission_tls_eof") from exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            raise GenerationNotSentError(f"generation request was not submitted: {reason}") from exc
        raise
    if bool(getattr(control, "cancelled", False)):
        cancel_lease()
        raise GenerationUnknownError("generation cancelled after submission started")
    try:
        response = active_connection.getresponse()
    except (OSError, http.client.HTTPException) as exc:
        cancel_lease()
        if control.cancelled:
            raise GenerationUnknownError("generation cancelled while waiting for response headers") from exc
        raise
    wrapped = _PooledResponse(response, lease)
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


def _proxy_pool_identity(proxy: str) -> str:
    """Return a stable pool key without embedding proxy credentials.

    The transport still uses the original proxy URL to establish the tunnel,
    but connection reuse and diagnostics must never retain its userinfo.
    Authentication is represented only by a non-reversible marker so proxies
    with and without credentials cannot accidentally share a connection.
    """
    if not proxy:
        return ""
    parsed = urlsplit(proxy)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    authority = host if port is None else f"{host}:{port}"
    if parsed.username is None:
        auth_marker = "anonymous"
    else:
        credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()
        auth_marker = "auth-" + hashlib.sha256(credentials).hexdigest()[:16]
    return f"{parsed.scheme.lower()}://{authority}|{auth_marker}"


class _SubmissionHTTPSHandler(urllib.request.HTTPSHandler):
    """Track generation bytes independently of TLS and proxy CONNECT bytes."""

    def __init__(self, submission: dict[str, bool], control: AttemptControl | None = None) -> None:
        # Loading the Windows certificate store can exceed a short generation
        # deadline. Plain HTTP requests must never pay this HTTPS setup cost.
        super().__init__()
        self.submission = submission
        self.control = control

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        request = req
        context = ssl.create_default_context()
        context.set_alpn_protocols(["http/1.1"])
        submission = self.submission
        tracked_control = self.control

        class Connection(http.client.HTTPSConnection):
            establishing = False
            _tracked_socket: Any = None

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
                    if tracked_control is not None:
                        tracked_control.mark_submission_started()
                super().send(data)

            def connect(self) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before connection")
                if tracked_control is not None:
                    tracked_control.register_connection(self)
                super().connect()
                self._tracked_socket = self.sock
                if tracked_control is not None and self._tracked_socket is not None:
                    tracked_control.register_connection(self._tracked_socket)
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before submission")

        submission["attempted"] = False
        return self.do_open(Connection, request, context=context)


class _SubmissionHTTPHandler(urllib.request.HTTPHandler):
    """Track plain HTTP sockets as well as TLS sockets."""

    def __init__(self, submission: dict[str, bool], control: AttemptControl | None = None) -> None:
        super().__init__()
        self.submission = submission
        self.control = control

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        request = req
        submission = self.submission
        tracked_control = self.control

        class Connection(http.client.HTTPConnection):
            establishing = False
            _tracked_socket: Any = None

            def connect(self) -> None:
                if tracked_control is not None and tracked_control.cancelled:
                    raise GenerationNotSentError("generation cancelled before connection")
                if tracked_control is not None:
                    tracked_control.register_connection(self)
                super().connect()
                self._tracked_socket = self.sock
                if tracked_control is not None and self._tracked_socket is not None:
                    tracked_control.register_connection(self._tracked_socket)
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
                    if tracked_control is not None:
                        tracked_control.mark_submission_started()
                super().send(data)

        submission["attempted"] = False
        return self.do_open(Connection, request)


def _open_request_sync(request: urllib.request.Request, timeout: float, *, control: AttemptControl | None = None,
                       attempt_budget: AttemptBudget | None = None,
                       http_session: Any = None, transport_options: Mapping[str, object] | None = None) -> Any:
    adapter = (transport_options or {}).get("opener")
    if callable(adapter):
        return adapter(request, timeout, control=control, transport_options=transport_options)
    _validate_proxy_scheme(transport_options, request.full_url)
    if http_session is not None:
        active_control = control or AttemptControl()
        return _open_pooled_request(request, timeout, control=active_control,
                                    http_session=http_session, transport_options=transport_options)
    # Transport performs exactly one physical attempt.  Whether a write-safe
    # TLS EOF may be retried belongs to the provider policy, which owns the
    # shared attempt budget and can make the same decision for pooled and
    # non-pooled connections.
    submission: dict[str, bool] = {}
    opener = urllib.request.build_opener(NoGenerationRedirect(), _SubmissionHTTPHandler(submission, control),
                                         _SubmissionHTTPSHandler(submission, control))
    try:
        return opener.open(request, timeout=max(0.001, timeout))
    except urllib.error.URLError as exc:
        if submission.get("attempted") is not False or not isinstance(exc.reason, ssl.SSLError):
            raise
        raise GenerationNotSentError(f"TLS connection failed before generation submission: {exc.reason}",
                                     reason_code="pre_submission_tls_eof" if isinstance(exc.reason, ssl.SSLEOFError)
                                     else "pre_submission_tls_error") from exc


def _selected_proxy(url: str, options: Mapping[str, object] | None) -> str:
    explicit = str((options or {}).get("proxy") or "")
    if explicit:
        return explicit
    parts = urlsplit(url)
    proxies = urllib.request.getproxies()
    candidate = proxies.get(parts.scheme) or proxies.get("all") or ""
    if candidate and parts.hostname and not urllib.request.proxy_bypass(parts.hostname):
        return candidate
    return ""


def _validate_proxy_scheme(options: Mapping[str, object] | None, url: str = "") -> None:
    proxy = _selected_proxy(url, options) if url else str((options or {}).get("proxy") or "")
    if not proxy:
        return
    scheme = urlsplit(proxy).scheme.lower()
    if scheme != "http":
        raise RequestPrepError(f"unsupported proxy scheme: {scheme or 'unknown'}")


def open_request(request: urllib.request.Request, timeout: float, *, control: AttemptControl | None = None,
                 attempt_budget: AttemptBudget | None = None,
                 http_session: Any = None, transport_options: Mapping[str, object] | None = None) -> Any:
    """Open one request while allowing Stop to leave a blocked header read."""
    if control is None:
        try:
            return _open_request_sync(request, timeout, attempt_budget=attempt_budget,
                                      http_session=http_session, transport_options=transport_options)
        except GenerationNotSentError as exc:
            # Preserve the small, public open_request seam used by older
            # callers that invoke it directly.  Provider exchanges always
            # supply an AttemptControl and therefore use the policy-owned
            # single-attempt path above.
            if (attempt_budget is None and http_session is None
                    and exc.reason_code == "pre_submission_tls_eof"):
                return _open_request_sync(request, timeout, transport_options=transport_options)
            raise
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
                if control.submission_started or control.submission_possible:
                    raise GenerationUnknownError("generation cancelled while opening request") from None
                raise GenerationNotSentError("generation cancelled before submission") from None
            if time.monotonic() >= deadline:
                control.cancel()
                if control.submission_started or control.submission_possible:
                    raise GenerationUnknownError("generation deadline exceeded while opening request") from None
                raise GenerationNotSentError("generation deadline exceeded before submission") from None
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
    abort = getattr(response, "abort", None)
    if callable(abort):
        with contextlib.suppress(Exception):
            abort()
        return
    # Shutdown first so close does not wait for a blocked buffered read.
    with contextlib.suppress(AttributeError, OSError):
        sock = _socket_for(response)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    if isinstance(response, urllib.error.HTTPError):
        # urllib keeps the live HTTPResponse in ``fp``.  Shutting down the
        # socket is not sufficient when the error wrapper was registered after
        # cancellation began; close that underlying response explicitly so a
        # blocked diagnostic-body read wakes immediately.
        body = getattr(response, "fp", None)
        if body is not None:
            with contextlib.suppress(Exception):
                body.close()
        return
    if isinstance(response, http.client.HTTPResponse):
        return
    if isinstance(response, http.client.HTTPConnection) and getattr(response, "sock", None) is None:
        return
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


def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], *, timeout: float,  # noqa: C901, PLR0912, PLR0915
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
        kwargs: dict[str, object] = {
            "control": control,
            "attempt_budget": attempt_budget,
            "http_session": http_session,
            "transport_options": {
                **(dict(transport_options) if transport_options is not None else {}),
                "body_idle_timeout": idle_timeout,
            },
        }
        try:
            response_context = cast(Any, opener)(request, timeout, **kwargs)
        except TypeError as exc:
            # A tiny compatibility seam for external deterministic transports
            # that still expose the original two-argument callable.  Only an
            # explicit unexpected-keyword error is eligible for fallback;
            # TypeErrors raised by the transport itself propagate unchanged.
            message = str(exc).lower()
            if "unexpected keyword argument" not in message:
                raise
            response_context = cast(Any, opener)(request, timeout)
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
                # Only use incremental reads when the response type declares
                # ``read1``. Dynamic test doubles and wrappers can synthesize
                # arbitrary instance attributes that are not byte readers.
                read1 = getattr(type(response), "read1", None)
                has_streaming_read = callable(read1)
                reader = BoundedBodyReader(
                    response,
                    control=control,
                    deadline=started + timeout,
                    idle_timeout=idle_timeout,
                    prefer_read1=has_streaming_read,
                )
                # JSON responses are framed by the HTTP content length (or
                # EOF); one bounded read preserves that framing while the
                # reader still records the actual bytes received.
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
            declared_length: int | None = None
            raw_length = exc.headers.get("Content-Length") if exc.headers is not None else None
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError):
                    declared_length = None
                if declared_length is not None and declared_length > MAX_ERROR_BODY_BYTES:
                    raise GenerationUnknownError("rejection body exceeds its diagnostic limit; not resent") from exc
            error_reader = BoundedBodyReader(
                exc, control=control, deadline=started + timeout,
                max_bytes=MAX_ERROR_BODY_BYTES + 1, prefer_read1=False,
                idle_timeout=idle_timeout,
            )
            detail_parts: list[bytes] = []
            detail_size = 0
            while detail_size <= MAX_ERROR_BODY_BYTES:
                chunk = error_reader.read_chunk(min(2000, MAX_ERROR_BODY_BYTES + 1 - detail_size))
                if not chunk:
                    break
                detail_parts.append(chunk)
                detail_size += len(chunk)
                if declared_length is not None and detail_size >= declared_length:
                    break
            raw_detail = b"".join(detail_parts)
            if len(raw_detail) > MAX_ERROR_BODY_BYTES:
                raise GenerationUnknownError("rejection body is incomplete or exceeds its diagnostic limit; not resent") from exc
            if declared_length is not None and len(raw_detail) != declared_length:
                raise GenerationUnknownError("rejection body framing is incomplete; not resent") from exc
            detail = raw_detail.decode("utf8", "replace")
        if errors.classify_http_error(exc.code, detail) == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError(detail[:400]) from exc
        request_id = exc.headers.get("X-Request-Id")
        extra_headers = (transport_options or {}).get("request_id_headers", ())
        if isinstance(extra_headers, (tuple, list)):
            request_id = request_id or next((exc.headers.get(name) for name in extra_headers
                                            if isinstance(name, str) and exc.headers.get(name)), None)
        raise GenerationRejectedError(
            exc.code,
            detail,
            exc.headers.get("Retry-After"),
            upstream_request_id=request_id,
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
