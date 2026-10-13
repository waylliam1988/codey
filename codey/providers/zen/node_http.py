"""Zen's desktop-compatible fetch transport; the shared policy owns retries."""
from __future__ import annotations

import base64
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from email.message import Message
from pathlib import Path
from typing import Any, cast

from codey.providers.api_transport import AttemptControl, GenerationNotSentError, GenerationUnknownError
from codey.providers.zen.identity import ZEN_BASE_URL


def node_runtime() -> str:
    # Playwright already supplies Node on every supported installation. Finding
    # its package does not import the browser stack or require a browser binary.
    spec = importlib.util.find_spec("playwright")
    if spec is not None and spec.origin:
        path = Path(spec.origin).parent / "driver" / ("node.exe" if os.name == "nt" else "node")
        if path.is_file():
            return str(path)
    path_text = shutil.which("node")
    if path_text:
        return path_text
    raise GenerationNotSentError("Zen requires the Node runtime bundled with Playwright")


class NodeResponse:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.headers = Message()
        self.status = 0
        self.reason = ""
        self._buffer = bytearray()
        self._eof = False
        self._abort_lock = threading.Lock()

    def frame(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = self.process.stdout.readline(128 * 1024)
        if not line or not line.endswith(b"\n"):
            raise GenerationUnknownError("Zen fetch stopped without a complete transport frame; not resent")
        try:
            frame = json.loads(line)
        except ValueError as exc:
            raise GenerationUnknownError("Zen fetch returned invalid transport framing; not resent") from exc
        if not isinstance(frame, dict):
            raise GenerationUnknownError("Zen fetch returned a non-object transport frame; not resent")
        if frame.get("type") == "error":
            if frame.get("requestSubmitted") is False and frame.get("connectionFailed") is True:
                network = frame.get("code") in {"UND_ERR_CONNECT_TIMEOUT", "ECONNREFUSED", "ENOTFOUND",
                                                "EAI_AGAIN", "ECONNRESET", "ETIMEDOUT"}
                raise GenerationNotSentError("Zen fetch failed before submission: " + str(frame.get("message"))[:400],
                    reason_code="pre_submission_network_error" if network else "not_submitted")
            raise GenerationUnknownError("Zen fetch outcome unknown; not resent: " + str(frame.get("message"))[:400])
        return frame

    def read1(self, size: int = 65536) -> bytes:
        while not self._buffer and not self._eof:
            frame = self.frame()
            if frame.get("type") == "end":
                self._eof = True
            elif frame.get("type") == "data" and isinstance(frame.get("data"), str):
                try:
                    self._buffer.extend(base64.b64decode(frame["data"], validate=True))
                except ValueError as exc:
                    raise GenerationUnknownError("Zen fetch returned invalid body bytes; not resent") from exc
            else:
                raise GenerationUnknownError("Zen fetch returned an unexpected body frame; not resent")
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def read(self, size: int = -1) -> bytes:
        data = bytearray()
        while size < 0 or len(data) < size:
            chunk = self.read1(65536 if size < 0 else min(65536, size - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)

    def abort(self) -> None:
        # Kill before closing buffered pipes: another thread can be reading.
        with self._abort_lock:
            if self.process.poll() is None:
                with contextlib.suppress(OSError):
                    self.process.kill()

    def close(self) -> None:
        self.abort()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.process.wait(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()

    def __enter__(self) -> NodeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_request(request: urllib.request.Request, timeout: float, *, control: AttemptControl | None = None,
                 transport_options: Mapping[str, object] | None = None) -> NodeResponse:
    if request.full_url not in {ZEN_BASE_URL + "/responses", ZEN_BASE_URL + "/chat/completions"}:
        raise GenerationNotSentError("Zen fetch destination is not allowed")
    active = control or AttemptControl()
    if not isinstance(request.data, (bytes, type(None))):
        raise GenerationNotSentError("Zen fetch requires a prepared byte body")
    if active.cancelled:
        raise GenerationNotSentError("Zen fetch cancelled before launch")
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    else:
        creationflags = 0
    process = subprocess.Popen([node_runtime(), str(Path(__file__).with_suffix(".mjs"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=creationflags)
    response = NodeResponse(process)
    active.register_connection(response)
    try:
        assert process.stdin is not None
        payload = {"url": request.full_url, "headers": dict(request.header_items()),
                   "body": base64.b64encode(request.data or b"").decode("ascii"), "timeout": timeout,
                   "idleTimeout": (transport_options or {}).get("body_idle_timeout")}
        active.mark_submission_possible()
        process.stdin.write(json.dumps(payload).encode() + b"\n")
        process.stdin.close()
        while True:
            frame = response.frame()
            if frame.get("type") == "submitted":
                active.mark_submission_started()
            elif frame.get("type") == "headers":
                response.status = int(frame["status"])
                response.reason = str(frame.get("reason") or "")
                for name, value in frame["headers"].items():
                    response.headers[name] = str(value)
                active.register_response(response)
                if response.status >= 300:
                    raise urllib.error.HTTPError(request.full_url, response.status, response.reason,
                                                 response.headers, cast(Any, response))
                return response
            else:
                raise GenerationUnknownError("Zen fetch returned unexpected opening frame; not resent")
    except urllib.error.HTTPError:
        raise
    except BaseException:
        response.close()
        raise
