from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from codey.codemode.types import CodeModeLimits, CodeModeResult, ToolCatalogEntry

ToolInvoker = Callable[[str, dict[str, object]], object]


class CodeModeRuntime(Protocol):
    def execute(
        self,
        code: str,
        *,
        catalog: Sequence[ToolCatalogEntry],
        invoke: ToolInvoker,
        state_store: Any = None,
        limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        """Run one program and mediate all host calls through ``invoke``."""


class NodeWorkerRuntime:
    """Run one program in a disposable Node worker with host mediated tools."""

    def __init__(self, *, node: str = "node", runner: Path | None = None) -> None:
        self.node = node
        self.runner = runner or Path(__file__).with_name("node_runner.mjs")

    def execute(
        self,
        code: str,
        *,
        catalog: Sequence[ToolCatalogEntry],
        invoke: ToolInvoker,
        state_store: Any = None,
        limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        active_limits = limits or CodeModeLimits()
        if len(code.encode("utf-8")) > active_limits.source_bytes:
            return CodeModeResult(False, error_code="source_limit", error="codemode source exceeds source byte limit")
        denied = re.search(
            r"\b(?:process|require|import|fetch|eval|globalThis|__dirname|__filename|child_process|fs)\b",
            code,
            re.IGNORECASE,
        ) or re.search(r"\bFunction\s*\(", code)
        if denied or re.search(r"tools\s*(?:\.\s*codemode|\[\s*['\"]codemode['\"]\s*\])", code, re.IGNORECASE):
            return CodeModeResult(False, error_code="sandbox_denied", error="direct host access is denied")
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                [self.node, "--max-old-space-size=128", str(self.runner)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            return CodeModeResult(False, error_code="runtime_unavailable", error=str(exc))
        stdin = process.stdin
        stdout = process.stdout
        assert stdin is not None
        assert stdout is not None
        messages: queue.Queue[dict[str, Any]] = queue.Queue()

        def read_messages() -> None:
            for line in stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    messages.put({"type": "error", "code": "protocol", "message": "worker emitted invalid JSON"})
                    continue
                if isinstance(message, dict):
                    messages.put(message)
            messages.put({"type": "eof"})

        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        request = {
            "type": "execute",
            "code": code,
            "catalog": [
                {"name": item.name, "description": item.description, "parameters": self._plain_json(item.parameters)}
                for item in catalog
            ],
            "limits": {
                "maxChildCalls": active_limits.max_child_calls,
                "maxConcurrentCalls": active_limits.max_concurrent_calls,
                "maxPendingPromises": active_limits.max_pending_promises,
                "maxOutputBytes": active_limits.max_output_bytes,
            },
            "stateEnabled": state_store is not None,
        }
        executor: ThreadPoolExecutor | None = None
        try:
            self._write_line(stdin, request)
            executor = ThreadPoolExecutor(max_workers=active_limits.max_concurrent_calls)
            return self._drive(
                process, stdin, messages, invoke, state_store, active_limits, started, executor,
            )
        finally:
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

    def _drive(
        self,
        process: subprocess.Popen[str],
        stdin: Any,
        messages: queue.Queue[dict[str, Any]],
        invoke: ToolInvoker,
        state_store: Any,
        limits: CodeModeLimits,
        started: float,
        executor: ThreadPoolExecutor,
    ) -> CodeModeResult:
        pending: dict[str, Future[dict[str, Any]]] = {}
        store_writes: dict[str, object] = {}
        while True:
            self._flush_completed(pending, stdin)
            elapsed = time.monotonic() - started
            if elapsed > limits.timeout_ms / 1000:
                process.kill()
                return CodeModeResult(False, error_code="timeout", error="codemode execution timed out")
            try:
                message = messages.get(timeout=min(0.1, limits.timeout_ms / 1000 - elapsed))
            except queue.Empty:
                continue
            kind = message.get("type")
            if kind == "call":
                call_id = str(message.get("callId", ""))
                if len(pending) >= limits.max_pending_promises:
                    self._write_line(stdin, {
                        "type": "result", "callId": call_id, "ok": False,
                        "errorCode": "pending_limit", "error": "codemode pending promise limit exceeded",
                    })
                else:
                    pending[call_id] = executor.submit(self._handle_call, message, invoke)
                continue
            if kind == "state":
                self._handle_state(message, stdin, state_store, store_writes)
                continue
            if kind == "done":
                return CodeModeResult(
                    bool(message.get("ok")), value=message.get("value"),
                    output=str(message.get("output", "") or ""),
                    error_code=str(message.get("errorCode", "") or ""),
                    error=str(message.get("error", "") or ""),
                    child_calls=int(message.get("childCalls", 0) or 0),
                    store_writes=store_writes,
                )
            if kind == "error":
                return CodeModeResult(False, error_code=str(message.get("code", "worker_error")), error=str(message.get("message", "worker error")))
            if kind == "eof":
                return CodeModeResult(False, error_code="worker_exit", error="codemode worker exited without a result")

    @staticmethod
    def _handle_state(
        message: dict[str, Any], stdin: Any, state_store: Any, store_writes: dict[str, object],
    ) -> None:
        call_id = str(message.get("callId", ""))
        key = str(message.get("key", ""))
        operation = str(message.get("op", ""))
        if state_store is None:
            NodeWorkerRuntime._write_line(
                stdin,
                {"type": "stateResult", "callId": call_id, "ok": False,
                 "errorCode": "state_unavailable", "error": "codemode state is unavailable"},
            )
            return
        try:
            if operation == "store":
                value = message.get("value")
                state_store.store(key, value)
                store_writes[key] = value
                response = {"type": "stateResult", "callId": call_id, "ok": True, "value": True}
            elif operation == "load":
                response = {
                    "type": "stateResult", "callId": call_id, "ok": True,
                    "value": state_store.load(key),
                }
            else:
                response = {"type": "stateResult", "callId": call_id, "ok": False,
                            "errorCode": "state_operation", "error": "unknown state operation"}
        except Exception as exc:
            response = {"type": "stateResult", "callId": call_id, "ok": False,
                        "errorCode": "state_error", "error": str(exc)[:400]}
        NodeWorkerRuntime._write_line(stdin, response, call_id=call_id)

    @staticmethod
    def _plain_json(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): NodeWorkerRuntime._plain_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [NodeWorkerRuntime._plain_json(item) for item in value]
        return value

    @staticmethod
    def _flush_completed(pending: dict[str, Future[dict[str, Any]]], stdin: Any) -> None:
        for call_id, future in tuple(pending.items()):
            if not future.done():
                continue
            pending.pop(call_id, None)
            try:
                response = future.result()
            except Exception as exc:
                response = {
                    "type": "result", "callId": call_id, "ok": False,
                    "errorCode": "host_execution_error", "error": str(exc)[:400],
                }
            NodeWorkerRuntime._write_line(stdin, response, call_id=call_id)

    @staticmethod
    def _write_line(stdin: Any, payload: dict[str, Any], *, call_id: str = "") -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            encoded = json.dumps({
                "type": "result", "callId": call_id, "ok": False,
                "errorCode": "host_result_encoding",
                "error": "host tool result is not JSON serializable",
            })
        stdin.write(encoded + "\n")
        stdin.flush()

    @staticmethod
    def _handle_call(
        message: dict[str, Any], invoke: ToolInvoker,
    ) -> dict[str, Any]:
        call_id = str(message.get("callId", ""))
        name = str(message.get("name", ""))
        args = message.get("args")
        if not isinstance(args, dict):
            return {"type": "result", "callId": call_id, "ok": False, "error": "tool args must be an object"}
        try:
            value = invoke(name, dict(args))
            return {"type": "result", "callId": call_id, "ok": True, "value": value}
        except Exception as exc:
            return {"type": "result", "callId": call_id, "ok": False, "error": str(exc)[:400]}
