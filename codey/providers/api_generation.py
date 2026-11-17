"""One bounded model generation using the sole transport and attempt budget."""
from __future__ import annotations

import contextlib
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Protocol, runtime_checkable

from codey.providers import api_transport as transport
from codey.providers.api_request_policy import AttemptBudget
from codey.providers.api_stream import ChatStreamAccumulator, DefaultChatAccumulator, read_sse
from codey.types.json_value import JsonObject, decode_json_object


class AttemptObserver(Protocol):
    def __call__(self, *, attempt: int, data: bytes, phase: str, response_bytes: int, seconds: float) -> None: ...


@runtime_checkable
class _ResponseContext(Protocol):
    def __enter__(self) -> object: ...
    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 traceback: TracebackType | None) -> bool | None: ...


@runtime_checkable
class _HeaderReader(Protocol):
    def get(self, name: str) -> object: ...


@runtime_checkable
class _AccumulatorFactory(Protocol):
    def __call__(self) -> ChatStreamAccumulator: ...


@runtime_checkable
class _ErrorDetailFilter(Protocol):
    def __call__(self, status: int, detail: str) -> str: ...


def _header(response: object, name: str) -> str | None:
    headers: object = getattr(response, "headers", None)
    if headers is None:
        return None
    if not isinstance(headers, _HeaderReader):
        raise transport.GenerationUnknownError("generation response has invalid headers; not resent")
    value = headers.get(name)
    if value is None or isinstance(value, str):
        return value
    raise transport.GenerationUnknownError("generation response has a non-text header; not resent")


def _content_length(response: object) -> int | None:
    # Chunk framing owns the body when Transfer-Encoding is present.
    if _header(response, "Transfer-Encoding"):
        return None
    raw = _header(response, "Content-Length")
    if raw is None:
        return None
    if not raw.strip().isascii() or not raw.strip().isdecimal():
        raise transport.GenerationUnknownError("generation response has invalid Content-Length; not resent")
    return int(raw)


def _accumulator_factory(options: Mapping[str, object]) -> _AccumulatorFactory:
    candidate = options.get("chat_accumulator_factory", DefaultChatAccumulator)
    if not isinstance(candidate, _AccumulatorFactory):
        raise TypeError("chat_accumulator_factory must declare a callable factory")
    return candidate


def _detail_filter(options: Mapping[str, object]) -> _ErrorDetailFilter | None:
    candidate = options.get("error_detail_filter")
    if candidate is None:
        return None
    if not isinstance(candidate, _ErrorDetailFilter):
        raise TypeError("error_detail_filter must declare a callable filter")
    return candidate


def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], *, timeout: float,  # noqa: C901, PLR0912, PLR0915
             observe: AttemptObserver, cancelled: Callable[[], bool] = lambda: False,
             opened: Callable[[object], None] = lambda response: None,
             on_event: Callable[[JsonObject], None] = lambda event: None,
             on_control: Callable[[transport.AttemptControl], None] = lambda control: None,
             attempt_budget: AttemptBudget | None = None,
             idle_timeout: float | None = None,
             http_session: object = None,
             transport_options: Mapping[str, object] | None = None) -> JsonObject:
    from codey.providers import error_classification as errors

    if cancelled():
        raise transport.GenerationNotSentError("generation cancelled before submission")
    data = json.dumps(payload, ensure_ascii=False).encode("utf8")
    request = urllib.request.Request(endpoint, data=data, headers=dict(headers), method="POST")
    started = time.monotonic()
    phase, count = "error", 0
    observe(attempt=1, data=data, phase="request", response_bytes=0, seconds=0.0)
    control = transport.AttemptControl()
    on_control(control)
    # The provider's cancellation callback is backed by a task-scoped event.
    # A socket read can block without reaching a Python callback, so poll the
    # callback in a tiny watcher and interrupt the response/control immediately.
    watcher_stop = threading.Event()

    def watch_cancellation() -> None:
        while not watcher_stop.wait(0.05):
            try:
                if cancelled():
                    control.cancel()
                    return
            except Exception:
                return

    watcher = threading.Thread(target=watch_cancellation, name="codey-api-cancel", daemon=True)
    watcher.start()
    deadline_guard = threading.Timer(max(0.001, started + timeout - time.monotonic()), control.cancel)
    deadline_guard.daemon = True
    deadline_guard.start()
    options: Mapping[str, object] = transport_options if transport_options is not None else {}

    def observe_event(event: object) -> None:
        on_event(decode_json_object(event))

    try:
        response_context: object
        try:
            response_context = transport.open_request(request, timeout, control=control,
                attempt_budget=attempt_budget, http_session=http_session,
                transport_options={**options, "body_idle_timeout": idle_timeout})
        except TypeError as exc:
            # A tiny compatibility seam for external deterministic transports
            # that still expose the original two-argument callable.  Only an
            # explicit unexpected-keyword error is eligible for fallback;
            # TypeErrors raised by the transport itself propagate unchanged.
            message = str(exc).lower()
            if "unexpected keyword argument" not in message:
                raise
            response_context = transport.open_request(request, timeout)
        if not isinstance(response_context, _ResponseContext):
            raise TypeError("generation transport requires a response context manager")
        with response_context as response:
            control.register_response(response)
            control.enter_phase("response_headers")
            opened(response)
            # A slow partial JSON body or SSE line can continuously reset the
            # socket timeout. Close the socket at the total deadline as well.
            if "text/event-stream" in (_header(response, "Content-Type") or ""):
                control.enter_phase("body")
                control.reset_body_timer()
                streamed: object = read_sse(response, deadline=started + timeout, cancelled=cancelled,
                                on_event=observe_event, control=control, idle_timeout=idle_timeout,
                                chat_accumulator_factory=_accumulator_factory(options),
                                error_detail_filter=_detail_filter(options))
                body = decode_json_object(streamed)
            else:
                control.enter_phase("body")
                control.reset_body_timer()
                frame_length = _content_length(response)
                if frame_length is not None and frame_length > transport.MAX_RESPONSE_BYTES:
                    raise transport.GenerationUnknownError("generation response exceeded its byte limit")
                # Only use incremental reads when the response type declares
                # ``read1``. Dynamic test doubles and wrappers can synthesize
                # arbitrary instance attributes that are not byte readers.
                read1: object = getattr(type(response), "read1", None)
                has_streaming_read = callable(read1)
                reader = transport.BoundedBodyReader(
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
                    raw = reader.read_chunk(transport.MAX_RESPONSE_BYTES + 1)
                count = len(raw)
                if count > transport.MAX_RESPONSE_BYTES:
                    raise transport.GenerationUnknownError("generation response exceeded its byte limit")
                if frame_length is not None and count != frame_length:
                    raise transport.GenerationUnknownError("generation response framing is incomplete; not resent")
                decoded: object = transport._json_object(raw)
                body = decode_json_object(decoded)
                on_event(body)
        if cancelled():
            raise transport.GenerationUnknownError("generation cancelled; late response discarded")
        if time.monotonic() >= started + timeout:
            raise transport.GenerationUnknownError("generation deadline exceeded; late response discarded")
        phase = "response"
        return body
    except transport.GenerationUnknownError:
        phase = "unknown"
        raise
    except transport.GenerationNotSentError:
        phase = "not_sent"
        raise
    except urllib.error.HTTPError as exc:
        control.register_response(exc)
        if control.cancelled or cancelled():
            raise transport.GenerationUnknownError("generation cancelled while reading rejection") from exc
        control.reset_body_timer()
        with contextlib.closing(exc):
            declared_length: int | None = None
            raw_length = _header(exc, "Content-Length")
            if raw_length:
                try:
                    declared_length = int(raw_length)
                except (TypeError, ValueError):
                    declared_length = None
                if declared_length is not None and declared_length > transport.MAX_ERROR_BODY_BYTES:
                    raise transport.GenerationUnknownError("rejection body exceeds its diagnostic limit; not resent") from exc
            error_reader = transport.BoundedBodyReader(
                exc, control=control, deadline=started + timeout,
                max_bytes=transport.MAX_ERROR_BODY_BYTES + 1, prefer_read1=False,
                idle_timeout=idle_timeout,
            )
            detail_parts: list[bytes] = []
            detail_size = 0
            while detail_size <= transport.MAX_ERROR_BODY_BYTES:
                chunk = error_reader.read_chunk(min(2000, transport.MAX_ERROR_BODY_BYTES + 1 - detail_size))
                if not chunk:
                    break
                detail_parts.append(chunk)
                detail_size += len(chunk)
                if declared_length is not None and detail_size >= declared_length:
                    break
            raw_detail = b"".join(detail_parts)
            if len(raw_detail) > transport.MAX_ERROR_BODY_BYTES:
                raise transport.GenerationUnknownError("rejection body is incomplete or exceeds its diagnostic limit; not resent") from exc
            if declared_length is not None and len(raw_detail) != declared_length:
                raise transport.GenerationUnknownError("rejection body framing is incomplete; not resent") from exc
            detail = raw_detail.decode("utf8", "replace")
        detail_filter = _detail_filter(options)
        if detail_filter is not None:
            detail = detail_filter(exc.code, detail)
        if errors.classify_http_error(exc.code, detail) == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError(detail[:400]) from exc
        request_id = _header(exc, "X-Request-Id")
        extra_headers = options.get("request_id_headers", ())
        if isinstance(extra_headers, (tuple, list)):
            names: tuple[object, ...] | list[object] = extra_headers
            request_id = request_id or next((value for name in names
                if isinstance(name, str) and (value := _header(exc, name))), None)
        raise transport.GenerationRejectedError(
            exc.code,
            detail,
            _header(exc, "Retry-After"),
            upstream_request_id=request_id,
        ) from exc
    except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
        reason: object = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            phase = "not_sent"
            raise transport.GenerationNotSentError(f"generation request was not submitted: {reason}") from exc
        phase = "unknown"
        raise transport.GenerationUnknownError(f"generation outcome unknown; request not resent: {exc}") from exc
    finally:
        watcher_stop.set()
        deadline_guard.cancel()
        control.finish()
        opened(None)
        observe(attempt=1, data=data, phase=phase, response_bytes=count, seconds=time.monotonic() - started)
