"""One bounded generation attempt. Unknown outcomes are never replayed."""
from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from codey.providers.diagnostics import (
    FAILURE_AUTHENTICATION_REQUIRED,
    FAILURE_RATE_LIMITED,
    FAILURE_REQUEST_REJECTED,
    FAILURE_TRANSIENT,
)

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class GenerationUnknownError(RuntimeError):
    provider_failure_kind = "submission_uncertain"


class GenerationNotSentError(RuntimeError):
    provider_failure_kind = "not_submitted"


class GenerationRejectedError(RuntimeError):
    """A complete HTTP failure response, distinct from an unknown generation."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.error_type = ""
        reason = detail
        with contextlib.suppress(ValueError, TypeError):
            body = json.loads(detail)
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                if isinstance(error.get("type"), str):
                    self.error_type = error["type"][:80]
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
        super().__init__(f"model HTTP {status}{label}: {reason[:400]}")


class NoGenerationRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class _SubmissionHTTPSHandler(urllib.request.HTTPSHandler):
    """Track generation bytes independently of TLS and proxy CONNECT bytes."""

    def __init__(self, submission: dict[str, bool]) -> None:
        self.context = ssl.create_default_context()
        self.context.set_alpn_protocols(["http/1.1"])
        super().__init__(context=self.context)
        self.submission = submission

    def https_open(self, request: Any) -> Any:
        submission = self.submission

        class Connection(http.client.HTTPSConnection):
            establishing = False

            def send(self, data: Any) -> None:
                if self.sock is None and self.auto_open:
                    self.establishing = True
                    try:
                        self.connect()
                    finally:
                        self.establishing = False
                if not self.establishing:
                    # Set before sendall: a failed write may have sent bytes.
                    submission["attempted"] = True
                super().send(data)

        submission["attempted"] = False
        return self.do_open(Connection, request, context=self.context)


def open_request(request: urllib.request.Request, timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    for attempt in range(2):
        submission: dict[str, bool] = {}
        opener = urllib.request.build_opener(NoGenerationRedirect(), _SubmissionHTTPSHandler(submission))
        try:
            return opener.open(request, timeout=max(0.001, deadline - time.monotonic()))
        except urllib.error.URLError as exc:
            if submission.get("attempted") is not False or not isinstance(exc.reason, ssl.SSLError):
                raise
            # TLS failed before HTTP generation headers/body. Reconnect once
            # for EOF only; certificate failures and exhausted budget stop.
            if isinstance(exc.reason, ssl.SSLEOFError) and attempt == 0 and time.monotonic() < deadline:
                continue
            raise GenerationNotSentError(f"TLS connection failed before generation submission: {exc.reason}") from exc
    raise AssertionError("unreachable connection retry state")


def abort_response(response: Any) -> None:
    if response is None:
        return
    # Shutdown first so close does not wait for a blocked buffered read.
    with contextlib.suppress(AttributeError, OSError):
        response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
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


def read_sse(response: Any, *, deadline: float, cancelled: Callable[[], bool]) -> dict[str, Any]:
    data: list[bytes] = []
    total = 0
    chat: dict[str, Any] = {"content": "", "tool_calls": []}
    calls: dict[int, dict[str, Any]] = {}
    chat_finish = ""
    chat_done = False
    while True:
        if cancelled():
            raise GenerationUnknownError("generation cancelled; late response discarded")
        if time.monotonic() >= deadline:
            raise GenerationUnknownError("generation deadline exceeded; not resent")
        line = response.readline(MAX_RESPONSE_BYTES + 1)
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


def generate(endpoint: str, payload: Mapping[str, object], headers: Mapping[str, str], *, timeout: float,
             observe: Callable[..., None], cancelled: Callable[[], bool] = lambda: False,
             opened: Callable[[Any], None] = lambda response: None) -> dict[str, Any]:
    from codey.providers import error_classification as errors

    if cancelled():
        raise GenerationNotSentError("generation cancelled before submission")
    data = json.dumps(payload, ensure_ascii=False).encode("utf8")
    request = urllib.request.Request(endpoint, data=data, headers=dict(headers), method="POST")
    started = time.monotonic()
    phase, count = "error", 0
    observe(attempt=1, data=data, phase="request", response_bytes=0, seconds=0.0)
    try:
        with open_request(request, timeout) as response:
            opened(response)
            # A slow partial JSON body or SSE line can continuously reset the
            # socket timeout. Close the socket at the total deadline as well.
            deadline_guard = threading.Timer(max(0.001, started + timeout - time.monotonic()), abort_response, args=(response,))
            deadline_guard.daemon = True
            deadline_guard.start()
            try:
                if "text/event-stream" in str(getattr(response, "headers", {}).get("Content-Type", "")):
                    body = read_sse(response, deadline=started + timeout, cancelled=cancelled)
                else:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    count = len(raw)
                    if count > MAX_RESPONSE_BYTES:
                        raise GenerationUnknownError("generation response exceeded its byte limit")
                    body = _json_object(raw)
            finally:
                deadline_guard.cancel()
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
        with contextlib.closing(exc):
            detail = exc.read(2000).decode("utf8", "replace")
        if errors.classify_http_error(exc.code, detail) == errors.ProviderErrorKind.CONTEXT_OVERFLOW:
            raise errors.ContextOverflowError(detail[:400]) from exc
        raise GenerationRejectedError(exc.code, detail) from exc
    except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
            phase = "not_sent"
            raise GenerationNotSentError(f"generation request was not submitted: {reason}") from exc
        phase = "unknown"
        raise GenerationUnknownError(f"generation outcome unknown; request not resent: {exc}") from exc
    finally:
        opened(None)
        observe(attempt=1, data=data, phase=phase, response_bytes=count, seconds=time.monotonic() - started)
