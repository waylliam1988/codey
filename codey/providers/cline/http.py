"""Bounded, cancellable JSON requests with safe diagnostics and no redirects."""
from __future__ import annotations

import contextlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from codey.providers import api_transport
from codey.providers.cline.identity import client_headers


@dataclass(frozen=True, repr=False)
class JsonReply:
    status: int
    body: dict[str, Any]


class JsonClient(Protocol):
    def request(self, url: str, body: dict[str, Any] | None = None, *, form: bool = False,
                deadline: float, cancelled: Callable[[], bool] = lambda: False) -> JsonReply: ...


class HttpClient:
    def request(self, url: str, body: dict[str, Any] | None = None, *, form: bool = False,
                deadline: float, cancelled: Callable[[], bool] = lambda: False) -> JsonReply:
        if cancelled() or time.monotonic() >= deadline:
            raise TimeoutError("Cline request cancelled or timed out")
        data = None if body is None else (urllib.parse.urlencode(body).encode() if form else json.dumps(body).encode())
        request = urllib.request.Request(url, data=data, headers={**client_headers(), "Content-Type":
            "application/x-www-form-urlencoded" if form else "application/json"})
        control = api_transport.AttemptControl()
        stop = threading.Event()

        def watch() -> None:
            while not stop.wait(0.05):
                if cancelled() or time.monotonic() >= deadline:
                    control.cancel()
                    return

        threading.Thread(target=watch, name="cline-control-http", daemon=True).start()
        try:
            try:
                response = api_transport.open_request(request, min(30.0, deadline - time.monotonic()), control=control)
            except urllib.error.HTTPError as exc:
                response = exc
            with contextlib.closing(response):
                control.register_response(response)
                reader = api_transport.BoundedBodyReader(response, control=control, deadline=deadline,
                                                        max_bytes=8 * 1024 * 1024)
                chunks: list[bytes] = []
                while True:
                    chunk = reader.read_chunk()
                    if not chunk:
                        break
                    chunks.append(chunk)
                value = json.loads(b"".join(chunks))
                if not isinstance(value, dict) or cancelled() or time.monotonic() >= deadline:
                    raise ValueError("invalid or late Cline response")
                status = response.status
                if type(status) is not int:
                    raise ValueError("invalid HTTP response status")
                return JsonReply(status, value)
        except (OSError, ValueError, api_transport.GenerationUnknownError):
            raise RuntimeError("Cline service request failed; retry.") from None
        finally:
            stop.set()
            control.finish()
