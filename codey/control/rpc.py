"""Bounded JSON-RPC 2.0 over strict UTF-8 JSONL; all work stays in the service."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import BinaryIO

from codey.control.client import ControlClient
from codey.control.protocol import COMMANDS, MAX_FRAME_BYTES, PROTOCOL_VERSION, ControlError
from codey.utils.object_fields import object_fields


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

    def _emit_error(
        self, identity: object, code: int, message: str, data: dict[str, object] | None = None,
    ) -> None:
        error: dict[str, object] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        frame: dict[str, object] = {"jsonrpc": "2.0", "id": identity, "error": error}
        self.emit(frame)

    def dispatch(self, request: dict[str, object]) -> None:
        identity = request.get("id")
        try:
            method = request.get("method")
            if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
                raise ControlError("INVALID_ARGUMENT", "Expected a JSON-RPC 2.0 request")
            if not (identity is None or type(identity) in (str, int)):
                raise ControlError("INVALID_ARGUMENT", "Request id must be a string or integer")
            no_params: dict[str, object] = {}
            params = request.get("params", no_params)
            result: dict[str, object]
            if method == "initialize":
                if identity is None:
                    raise ControlError("INVALID_ARGUMENT", "Initialization requires a response id")
                if not object_fields(params):
                    raise ControlError("INVALID_ARGUMENT", "Initialization parameters must be an object")
                if params.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
                    raise ControlError("PROTOCOL_MISMATCH", "Incompatible control protocol")
                self.initialized = True
                commands: dict[str, dict[str, object]] = {}
                for name, command in COMMANDS.items():
                    schema: dict[str, object] = command.schema()
                    commands[name] = {"description": command.description, "input_schema": schema, "readonly": command.readonly}
                result = {"protocol_version": PROTOCOL_VERSION, "commands": commands}
            elif method in {"notifications/initialized", "notifications/cancelled"}:
                if method.endswith("cancelled") and object_fields(params):
                    cancelled_id = params.get("requestId")
                    if type(cancelled_id) in (str, int):
                        with self.lock:
                            self.cancelled.add(cancelled_id)
                return
            else:
                if not self.initialized:
                    raise ControlError("PROTOCOL_MISMATCH", "Initialize this adapter before calling commands")
                if identity is None:
                    raise ControlError("INVALID_ARGUMENT", "Control commands require an id")
                name = method.removeprefix("control.")
                result = self.client.call(name, params)
            with self.lock:
                if identity in self.cancelled:
                    self.cancelled.discard(identity)
                    return
            frame: dict[str, object] = {"jsonrpc": "2.0", "id": identity, "result": result}
            self.emit(frame)
        except ControlError as exc:
            if identity is not None:
                data: dict[str, object] = exc.payload()
                self._emit_error(identity, -32602 if exc.code == "INVALID_ARGUMENT" else -32000, str(exc), data)
        except Exception:
            if identity is not None:
                self._emit_error(identity, -32603, "Control adapter failed")
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
                    request: object = json.loads(raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8"))
                    if not object_fields(request):
                        raise ValueError("Expected an object")
                except ValueError:
                    self._emit_error(None, -32700, "Invalid UTF-8 JSON frame")
                    continue
                if not self.slots.acquire(blocking=False):
                    self._emit_error(request.get("id"), -32000, "Too many outstanding requests", {"code": "RATE_LIMITED"})
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
    output: BinaryIO = sys.stdout.buffer
    stream: BinaryIO = sys.stdin.buffer
    return RpcAdapter(client, output).serve(stream)
