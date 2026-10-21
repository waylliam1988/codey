"""Bounded JSON-RPC 2.0 over strict UTF-8 JSONL; all work stays in the service."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO

from codey.control.client import ControlClient
from codey.control.protocol import COMMANDS, MAX_FRAME_BYTES, PROTOCOL_VERSION, ControlError


class RpcAdapter:
    def __init__(self, client: ControlClient, output: BinaryIO) -> None:
        self.client, self.output = client, output
        self.initialized = False
        self.closed = threading.Event()
        self.messages: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self.slots = threading.BoundedSemaphore(16)
        self.cancelled: set[object] = set()
        self.lock = threading.Lock()
        self.writer = threading.Thread(target=self._write, name="rpc-output", daemon=True)
        self.writer.start()

    def _write(self) -> None:
        while (data := self.messages.get()) is not None:
            # Only the stdio adapter exits on its own blocked pipe; the shared
            # service and all admitted tasks are independent of this process.
            watchdog = threading.Timer(10, lambda: os._exit(74))
            watchdog.daemon = True
            watchdog.start()
            try:
                self.output.write(data)
                self.output.flush()
            except OSError:
                self.closed.set()
                return
            finally:
                watchdog.cancel()

    def emit(self, value: object) -> None:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode() + b"\n"
        if len(data) > MAX_FRAME_BYTES:
            data = b'{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Response exceeds frame budget"}}\n'
        try:
            self.messages.put_nowait(data)
        except queue.Full:
            self.closed.set()

    def dispatch(self, request: dict[str, Any]) -> None:
        identity = request.get("id")
        try:
            if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise ControlError("INVALID_ARGUMENT", "Expected a JSON-RPC 2.0 request")
            if not (identity is None or type(identity) in (str, int)):
                raise ControlError("INVALID_ARGUMENT", "Request id must be a string or integer")
            method = request["method"]
            if method == "initialize":
                if identity is None:
                    raise ControlError("INVALID_ARGUMENT", "Initialization requires a response id")
                params = request.get("params", {})
                if params.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
                    raise ControlError("PROTOCOL_MISMATCH", "Incompatible control protocol")
                self.initialized = True
                result = {"protocol_version": PROTOCOL_VERSION, "commands": {
                    name: {"description": command.description, "input_schema": command.schema(), "readonly": command.readonly}
                    for name, command in COMMANDS.items()}}
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                if method.endswith("cancelled"):
                    with self.lock:
                        self.cancelled.add(request.get("params", {}).get("requestId"))
                return
            else:
                if not self.initialized:
                    raise ControlError("PROTOCOL_MISMATCH", "Initialize this adapter before calling commands")
                if identity is None:
                    raise ControlError("INVALID_ARGUMENT", "Control commands require an id")
                name = method.removeprefix("control.")
                result = self.client.call(name, request.get("params", {}))
            with self.lock:
                if identity in self.cancelled:
                    self.cancelled.discard(identity)
                    return
            self.emit({"jsonrpc": "2.0", "id": identity, "result": result})
        except ControlError as exc:
            if identity is not None:
                self.emit({"jsonrpc": "2.0", "id": identity,
                           "error": {"code": -32602 if exc.code == "INVALID_ARGUMENT" else -32000,
                                     "message": str(exc), "data": exc.payload()}})
        except Exception:
            if identity is not None:
                self.emit({"jsonrpc": "2.0", "id": identity, "error": {"code": -32603, "message": "Control adapter failed"}})
        finally:
            self.slots.release()

    def serve(self, stream: BinaryIO) -> int:
        pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="rpc-command")
        try:
            while not self.closed.is_set():
                raw = stream.readline(MAX_FRAME_BYTES + 2)
                if not raw:
                    break
                if len(raw) > MAX_FRAME_BYTES:
                    return 65
                if not raw.endswith(b"\n"):
                    return 65  # An incomplete modification frame never executes.
                try:
                    request = json.loads(raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("Expected an object")
                except (ValueError, UnicodeDecodeError):
                    self.emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid UTF-8 JSON frame"}})
                    continue
                if not self.slots.acquire(blocking=False):
                    self.emit({"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": "Too many outstanding requests", "data": {"code": "RATE_LIMITED"}}})
                    continue
                if request.get("method") == "initialize":
                    self.dispatch(request)
                else:
                    pool.submit(self.dispatch, request)
            return 0
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            if not self.closed.is_set():
                self.messages.put(None, timeout=1)
                self.writer.join(timeout=2)


def run(client: ControlClient) -> int:
    return RpcAdapter(client, sys.stdout.buffer).serve(sys.stdin.buffer)
