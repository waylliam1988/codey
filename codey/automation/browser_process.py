"""Synchronous facade transport with a separate control reader and bounded cancellation."""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from codey.automation.browser_binding import BrowserBinding
from codey.automation.browser_contracts import (
    MAX_PENDING_COMMANDS,
    BrowserCommand,
    BrowserCompletion,
    BrowserConfig,
    BrowserToolOutput,
)
from codey.automation.browser_ipc import BrowserPacket, read_packet, write_packet
from codey.policies.network import check_fetch_url
from codey.runtime.core import cancellation
from codey.types.json_value import (
    JsonObject,
    decode_json_object,
    require_json_boolean,
    require_json_integer,
    require_json_string,
)

NetworkPolicy = Callable[[JsonObject], JsonObject]
Progress = Callable[[JsonObject], None]


@dataclass
class Pending:
    future: Future[BrowserPacket] = field(default_factory=Future)
    progress: JsonObject = field(default_factory=lambda: {"phase": "queued", "dispatch": "not_sent"})
    observer: Progress | None = None


class BrowserProcess:
    def __init__(self, config: BrowserConfig, owner_task_id: str, resource_id: str, *,
                 network_policy: NetworkPolicy | None = None, binding: BrowserBinding | None = None) -> None:
        self.config, self.owner_task_id, self.resource_id = config, owner_task_id, resource_id
        self.network_policy = network_policy
        self.binding = binding
        self.generation = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._pending: dict[str, Pending] = {}
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._write_lock = threading.Lock()
        self._ready: Future[None] = Future()
        self._reader: threading.Thread | None = None
        self._routes: ThreadPoolExecutor | None = None
        self._error = ""
        self._released = False
        self._closed: JsonObject | None = None
        self.recovery_inputs: JsonObject = {}

    def _start(self) -> None:
        with self._lifecycle:
            if self._released:
                raise RuntimeError("browser_resource_released")
            if self._process is not None and (self._error or self._process.poll() is not None):
                self.close(release=False)
            if self._process is not None:
                return
            self._error = ""
            self._closed = None
            self._ready = Future()
            routes = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task-browser-network")
            try:
                process = subprocess.Popen([sys.executable, "-m", "codey.automation.browser_worker_main"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except BaseException:
                routes.shutdown(wait=False, cancel_futures=True)
                raise
            self._routes = routes
            self._process = process
            try:
                self._send(BrowserPacket({"type": "init", "config": self.config.payload(),
                    "owner_task_id": self.owner_task_id, "resource_id": self.resource_id,
                    "inputs": self.recovery_inputs,
                    "binding": self.binding.payload() if self.binding is not None else None}))
                reader = threading.Thread(target=self._read, args=(process, routes), name="task-browser-control", daemon=True)
                reader.start()
            except BaseException:
                # The child may still be waiting for init, so it cannot honor
                # a graceful close message yet.
                with contextlib.suppress(OSError):
                    process.terminate()
                self._close(release=False)
                raise
            self._reader = reader
        try:
            self._ready.result(timeout=10)
        except (TimeoutError, RuntimeError) as exc:
            self.close()
            raise RuntimeError("browser_worker_startup_failed: " + str(exc)) from exc

    def restore(self, binding: BrowserBinding | None) -> None:
        if self._process is None:
            self.binding = binding

    def attach_owner(self, owner_task_id: str) -> None:
        if self._process is not None and self.owner_task_id != owner_task_id:
            raise ValueError("a live browser cannot move to another task owner")
        self.owner_task_id = owner_task_id

    def _send(self, packet: BrowserPacket, *, expected: subprocess.Popen[bytes] | None = None) -> None:
        with self._write_lock:
            process = self._process
            if (process is None or expected is not None and process is not expected
                    or process.stdin is None or process.stdin.closed or process.poll() is not None):
                raise RuntimeError("browser_worker_unavailable")
            write_packet(process.stdin, packet)

    def _route(self, packet: BrowserPacket, process: subprocess.Popen[bytes]) -> None:
        request = decode_json_object(packet.message["request"])
        try:
            if self.network_policy is None:
                reason = check_fetch_url(require_json_string(request["url"], "url"))
                decision: JsonObject = {"decision": "abort" if reason else "continue"}
            else:
                decision = self.network_policy(request)
        except (OSError, RuntimeError, ValueError):
            decision = {"decision": "abort"}
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            self._send(BrowserPacket({"type": "route_result", "route_id": packet.message["route_id"], "decision": decision}), expected=process)

    def _read(self, process: subprocess.Popen[bytes], routes: ThreadPoolExecutor) -> None:
        if process.stdout is None:
            return
        try:
            while True:
                packet = read_packet(process.stdout)
                message = packet.message
                kind = message.get("type")
                if kind == "ready":
                    self._ready.set_result(None)
                elif kind == "route":
                    routes.submit(self._route, packet, process)
                elif kind in {"result", "progress"}:
                    identity = require_json_string(message["operation_id"], "operation_id")
                    with self._lock:
                        pending = self._pending.get(identity)
                    if pending is None:
                        continue
                    if "binding" in message and message["binding"] is not None:
                        self.binding = BrowserBinding.read(message["binding"])
                    if kind == "progress":
                        pending.progress = message
                        self.observe_input_state(message)
                        if pending.observer is not None:
                            pending.observer(message)
                        self._send(BrowserPacket({"type": "progress_ack", "progress_id": message["progress_id"]}), expected=process)
                    elif not pending.future.done():
                        self.generation = require_json_integer(message["generation"], "generation")
                        pending.future.set_result(packet)
                elif kind == "closed":
                    self._closed = message
                elif kind != "health":
                    raise ValueError("invalid browser worker message")
        except (EOFError, OSError, RuntimeError, ValueError) as exc:
            if process is not self._process:
                return
            self._error = str(exc)
            if not self._ready.done():
                self._ready.set_exception(RuntimeError(self._error))
            with self._lock:
                active = tuple(self._pending.values())
            for pending in active:
                if not pending.future.done():
                    pending.future.set_exception(RuntimeError("browser_worker_lost"))

    def observe_input_state(self, progress: JsonObject) -> None:
        document, inputs = progress.get("document"), progress.get("input_state")
        if isinstance(document, dict) and isinstance(inputs, dict):
            target = require_json_string(document["target_id"], "input target")
            self.recovery_inputs[target] = inputs

    @staticmethod
    def _failed(command: BrowserCommand, pending: Pending, reason: str) -> BrowserPacket:
        output = BrowserToolOutput("ERROR: " + reason, False, {"action": command.action.value,
            "reason": reason, "phase": pending.progress.get("phase", "queued"),
            "ref": command.arguments.get("ref", ""),
            "document": pending.progress.get("document"), "query_baseline": pending.progress.get("query_baseline"),
            "input_state": pending.progress.get("input_state"),
            "delivery": "not_delivered" if pending.progress.get("dispatch") == "not_sent" else "uncertain"})
        return BrowserPacket({"value": output.payload(), "operation_id": command.operation_id,
                              "completion": BrowserCompletion.FAILED.value})

    def call(self, command: BrowserCommand, *, binary: bytes = b"", progress: Progress | None = None) -> BrowserPacket:
        pending = Pending(observer=progress)
        try:
            self._start()
        except (OSError, RuntimeError) as exc:
            return self._failed(command, pending, str(exc))
        with self._lock:
            if command.operation_id in self._pending:
                raise ValueError("duplicate browser operation")
            if len(self._pending) >= MAX_PENDING_COMMANDS:
                return self._failed(command, pending, "browser_queue_full")
            self._pending[command.operation_id] = pending
        cancelled_at: float | None = None
        try:
            self._send(BrowserPacket({"type": "dispatch", "command": command.payload()}, binary))
            while not pending.future.done():
                try:
                    cancellation.check()
                    if time.monotonic() >= command.deadline:
                        raise cancellation.DeadlineExceeded("browser command deadline")
                except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                    if cancelled_at is None:
                        cancelled_at = time.monotonic()
                        self._send(BrowserPacket({"type": "cancel", "operation_id": command.operation_id}))
                if cancelled_at is not None and time.monotonic() - cancelled_at > 2:
                    self.close(release=False)
                    return self._failed(command, pending, "browser_cancel_unacknowledged")
                threading.Event().wait(0.02)
            return pending.future.result()
        except (OSError, RuntimeError) as exc:
            return self._failed(command, pending, str(exc))
        finally:
            with self._lock:
                self._pending.pop(command.operation_id, None)

    def close(self, *, release: bool = True) -> None:
        with self._lifecycle:
            if self._released:
                return
            self._close(release=release)
            self._released = release

    def _close(self, *, release: bool) -> None:
        process = self._process
        if release and process is not None and (process.poll() is not None or self._error):
            self._close(release=False)
            process = None
        if process is None:
            if not release or self.binding is None or not (self.binding.owned_browser or any(page.owned for page in self.binding.pages)):
                return
            self._start()
            process = self._process
            if process is None:
                raise RuntimeError("browser cleanup worker unavailable")
        with contextlib.suppress(OSError, RuntimeError):
            self._send(BrowserPacket({"type": "close", "release": release}))
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        if self._routes is not None:
            self._routes.shutdown(wait=False, cancel_futures=True)
        self._process = None
        self._reader = None
        self._routes = None
        if release and (self._closed is None or not require_json_boolean(self._closed.get("ok"), "cleanup ok")):
            raise RuntimeError("browser_cleanup_failed: " + str((self._closed or {}).get("reason", "worker closed without cleanup receipt")))
