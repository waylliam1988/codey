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
from typing import Any, cast

from codey.providers import api_transport as transport
from codey.providers.api_request_policy import AttemptBudget
from codey.providers.api_stream import ChatStreamAccumulator, DefaultChatAccumulator, read_sse


def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], *, timeout: float,  # noqa: C901, PLR0912, PLR0915
             observe: Callable[..., None], cancelled: Callable[[], bool] = lambda: False,
             opened: Callable[[Any], None] = lambda response: None,
             on_event: Callable[[dict[str, Any]], None] = lambda event: None,
             on_control: Callable[[transport.AttemptControl], None] = lambda control: None,
             attempt_budget: AttemptBudget | None = None,
             idle_timeout: float | None = None,
             http_session: Any = None,
             transport_options: Mapping[str, object] | None = None) -> dict[str, Any]:
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
    try:
        opener = transport.open_request
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
                                on_event=on_event, control=control, idle_timeout=idle_timeout,
                                chat_accumulator_factory=cast(Callable[[], ChatStreamAccumulator],
                                    (transport_options or {}).get("chat_accumulator_factory", DefaultChatAccumulator)))
            else:
                control.enter_phase("body")
                control.reset_body_timer()
                # Only use incremental reads when the response type declares
                # ``read1``. Dynamic test doubles and wrappers can synthesize
                # arbitrary instance attributes that are not byte readers.
                read1 = getattr(type(response), "read1", None)
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
                body = transport._json_object(raw)
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
            raw_length = exc.headers.get("Content-Length") if exc.headers is not None else None
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
        detail_filter = (transport_options or {}).get("error_detail_filter")
        if callable(detail_filter):
            detail = str(detail_filter(exc.code, detail))
        if errors.classify_http_error(exc.code, detail) == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError(detail[:400]) from exc
        request_id = exc.headers.get("X-Request-Id")
        extra_headers = (transport_options or {}).get("request_id_headers", ())
        if isinstance(extra_headers, (tuple, list)):
            request_id = request_id or next((exc.headers.get(name) for name in extra_headers
                                            if isinstance(name, str) and exc.headers.get(name)), None)
        raise transport.GenerationRejectedError(
            exc.code,
            detail,
            exc.headers.get("Retry-After"),
            upstream_request_id=request_id,
        ) from exc
    except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
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
