"""Loopback-only, streaming HTTP observer with explicit reproducible fault injection."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


def local_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("experiment endpoints must be plain loopback HTTP URLs")
    if parsed.hostname != "localhost" and not ipaddress.ip_address(parsed.hostname or "").is_loopback:
        raise ValueError("experiment endpoints must be loopback addresses")
    return url.rstrip("/")


def safe_json(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        last = None
        usage = None
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.startswith("data:"):
                continue
            with suppress(json.JSONDecodeError):
                value = json.loads(line[5:].strip())
                if isinstance(value, dict):
                    last = value
                    if isinstance(value.get("usage"), dict):
                        usage = value
        return usage or last or {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects are forbidden in a loopback experiment")


class Observer(BaseHTTPRequestHandler):
    server_version = "CodeyAgentStabilityObserver/1"

    def log_message(self, *args):
        pass

    def _inject(self, payload):
        proxy = self.server
        if urlsplit(self.path).path != "/v1/chat/completions":
            return None
        with proxy.record_lock:
            proxy.generation_requests += 1
            if proxy.generation_requests > proxy.request_limit:
                return 429, {"error": {"message": "experiment generation-request budget exhausted"}}
            if proxy.fault_used or not proxy.fault:
                return None
            proxy.fault_used = True
        if proxy.fault == "http-503":
            return 503, {"error": {"message": "experiment transient overload", "type": "server_error"}}
        if proxy.fault == "truncated-call":
            tools = payload.get("tools", []) if isinstance(payload, dict) else []
            edit = next((t.get("function", {}).get("name") for t in tools
                         if t.get("function", {}).get("name") in {"edit", "write", "write_file"}), "edit")
            return 200, {"id": "injected-truncated-call", "object": "chat.completion", "choices": [{
                "index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "incomplete-edit", "type": "function", "function": {
                    "name": edit, "arguments": '{"path":"app.py",'}}]}}]}
        raise ValueError(f"unknown fault {proxy.fault}")

    def _forward(self):
        proxy = self.server
        arm = proxy.active_arm  # Freeze identity before the upstream request starts.
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = safe_json(body)
        started = time.perf_counter()
        response_bytes = bytearray()
        count = 0
        first_byte = None
        status, error, synthetic = 599, "", False
        headers_sent = False
        request_id = uuid4().hex
        proxy.record({"request_id": request_id, "arm": arm, "method": self.command,
                      "path": urlsplit(self.path).path, "request": payload, "response_complete": False})
        try:
            injected = self._inject(payload)
            if injected is not None:
                synthetic = True
                status, response = injected
                stream = isinstance(payload, dict) and payload.get("stream") is True and status == 200
                if stream:
                    choice = response["choices"][0]
                    response = {**response, "object": "chat.completion.chunk", "choices": [{
                        "index": 0, "delta": choice["message"], "finish_reason": choice["finish_reason"]}]}
                raw = (f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n".encode() if stream
                       else json.dumps(response).encode())
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
                self.send_header("Retry-After", "0")
                self.end_headers()
                headers_sent = True
                self.wfile.write(raw)
                self.wfile.flush()
                response_bytes.extend(raw)
                count = len(raw)
            else:
                request = Request(proxy.upstream + self.path, data=body or None, method=self.command)
                for name in ("Content-Type", "Authorization"):
                    if self.headers.get(name):
                        request.add_header(name, self.headers[name])
                try:
                    response = build_opener(NoRedirect()).open(request, timeout=proxy.timeout)
                except HTTPError as exc:
                    response = exc  # Preserve status/body/Retry-After, including rejected requests.
                with response:
                    status = int(response.code)
                    self.send_response(status)
                    for name in ("Content-Type", "Retry-After"):
                        if response.headers.get(name):
                            self.send_header(name, response.headers[name])
                    self.end_headers()
                    headers_sent = True
                    while True:
                        chunk = response.read1(4096)
                        if not chunk:
                            break
                        if first_byte is None:
                            first_byte = time.perf_counter() - started
                        count += len(chunk)
                        if len(response_bytes) < 16 * 1024 * 1024:
                            response_bytes.extend(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if not headers_sent:
                with suppress(OSError):
                    self.send_response(502)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": error}).encode())
        finally:
            proxy.record({"request_id": request_id, "arm": arm, "method": self.command, "path": urlsplit(self.path).path,
                          "request": payload, "status": status, "response": safe_json(bytes(response_bytes)),
                          "synthetic": synthetic, "response_complete": not error,
                          "capture_truncated": count > len(response_bytes), "error": error,
                          "first_byte_seconds": first_byte, "wall_time_seconds": time.perf_counter() - started})

    def do_GET(self):  # noqa: N802
        self._forward()

    def do_POST(self):  # noqa: N802
        self._forward()


class Proxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, upstream, timeout, *, fault="", journal: Path | None = None,
                 request_limit=32):
        super().__init__(address, Observer)
        self.upstream = local_url(upstream)
        self.timeout = timeout
        self.active_arm = ""
        self.records = []
        self.record_lock = threading.Lock()
        self.fault = fault
        self.fault_used = False
        self.generation_requests = 0
        self.request_limit = request_limit
        self.journal = journal

    def record(self, row):
        with self.record_lock:
            prior = next((r for r in self.records if r["request_id"] == row["request_id"]), None)
            if prior is None:
                self.records.append(row)
            else:
                prior.update(row)
            if self.journal:
                with self.journal.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
