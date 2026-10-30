from __future__ import annotations

import hashlib
import json
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from codey.codemode.identity import rpc_child_index
from codey.codemode.protocol import CodeModeInputError, parse_codemode_args
from codey.codemode.types import CodeModeLimits, CodeModeProgram, CodeModeResult, ToolCatalogEntry
from codey.runtime.core import cancellation

ToolInvoker = Callable[[str, dict[str, object]], object]


class CodeModeRuntime(Protocol):
    def contract_digest(self) -> str:
        """Identify the engine and execution semantics used for recovery."""

    def execute(
        self,
        code: str,
        *,
        catalog: Sequence[ToolCatalogEntry],
        invoke: ToolInvoker,
        inputs: Mapping[str, object] | None = None,
        state_store: Any = None,
        replay_context: Mapping[str, object] | None = None,
        limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        """Run one program and mediate all host calls through ``invoke``."""


class NodeWorkerRuntime:
    """Run a QuickJS/WASM guest in disposable Node transport, mediating tools."""

    def __init__(self, *, node: str = "node", runner: Path | None = None) -> None:
        self.node = shutil.which(node) or node
        self.runner = runner or Path(__file__).with_name("node_runner.mjs")

    def contract_digest(self) -> str:
        """Bind recovery to the worker, actual engine and ambient JS semantics."""
        probe = subprocess.run([self.node, "-p", "JSON.stringify({node:process.version,v8:process.versions.v8,"
            "icu:process.versions.icu,unicode:process.versions.unicode,platform:process.platform,arch:process.arch,"
            "timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,"
            "locale:Intl.DateTimeFormat().resolvedOptions().locale})"],
            check=True, capture_output=True, text=True, encoding="utf-8", timeout=5)
        assets = Path(__file__).parent
        dependencies = [assets / "guest_runner.js", *sorted((assets / "quickjs").glob("*.js")),
                        assets / "quickjs" / "quickjs.wasm", assets / "quickjs" / "package.json"]
        contract = {"schema": 2, "runner": hashlib.sha256(self.runner.read_bytes()).hexdigest(),
                    "sandbox": {path.relative_to(assets).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in dependencies}, "memory_bytes": 64 * 1024 * 1024,
                    "engine": json.loads(probe.stdout), "rpc": 1, "determinism": 1,
                    "result_delivery": "issuance_order", "heap_mb": 128, "timeout": "per_attempt"}
        return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def execute(
        self,
        code: str,
        *,
        catalog: Sequence[ToolCatalogEntry],
        invoke: ToolInvoker,
        inputs: Mapping[str, object] | None = None,
        state_store: Any = None,
        replay_context: Mapping[str, object] | None = None,
        limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        cancellation.check()
        active_limits = limits or CodeModeLimits()
        try:
            program = parse_codemode_args({"code": code, "inputs": {} if inputs is None else inputs}, limits=active_limits)
        except CodeModeInputError as exc:
            return CodeModeResult(False, error_code=exc.code, error=str(exc))
        with cancellation.linked_scope(deadline=time.monotonic() + active_limits.timeout_ms / 1000) as stop:
            try:
                return self._run_worker(program, catalog, invoke, state_store, active_limits, stop, replay_context)
            except cancellation.DeadlineExceeded:
                return CodeModeResult(False, error_code="timeout", error="codemode execution timed out")

    def _run_worker(
        self, program: CodeModeProgram, catalog: Sequence[ToolCatalogEntry], invoke: ToolInvoker,
        state_store: Any, limits: CodeModeLimits, stop: threading.Event,
        replay_context: Mapping[str, object] | None,
    ) -> CodeModeResult:
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
        messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=limits.max_pending_promises + 8)

        def publish(message: dict[str, Any]) -> None:
            while not stop.is_set():
                try:
                    messages.put(message, timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_messages() -> None:
            while not stop.is_set():
                line = stdout.readline(limits.max_rpc_bytes + 1)
                if not line:
                    break
                if len(line.encode("utf-8")) > limits.max_rpc_bytes:
                    publish({"type": "error", "code": "rpc_limit", "message": "worker RPC message exceeds its byte limit"})
                    return
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    publish({"type": "error", "code": "protocol", "message": "worker emitted invalid JSON"})
                    continue
                if isinstance(message, dict):
                    publish(message)
            publish({"type": "eof"})

        reader = threading.Thread(target=read_messages, daemon=True)
        request = {
            "type": "execute",
            "code": program.code,
            "inputs": self._plain_json(program.inputs),
            "catalog": [
                {"name": item.name, "description": item.description, "parameters": self._plain_json(item.parameters)}
                for item in catalog
            ],
            "limits": {
                "timeoutMs": limits.timeout_ms,
                "maxChildCalls": limits.max_child_calls,
                "maxConcurrentCalls": limits.max_concurrent_calls,
                "maxPendingPromises": limits.max_pending_promises,
                "maxOutputBytes": limits.max_output_bytes,
            },
            "stateEnabled": state_store is not None,
            "determinism": dict(replay_context) if replay_context is not None else {"epochMs": int(time.time() * 1000), "seed": 1},
        }
        try:
            reader.start()
            # Host callbacks re-enter execute_turn; this pool owns no facts.
            with ThreadPoolExecutor(max_workers=limits.max_concurrent_calls) as executor:
                try:
                    self._write_line(stdin, request)
                    return self._drive(stdin, messages, invoke, state_store, limits, executor, stop,
                                       {entry.name for entry in catalog if not entry.read_only})
                finally:
                    # Join running callbacks and reject queued work through the
                    # same cancellation context, before releasing the parent.
                    stop.set()
        finally:
            stop.set()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            stdin.close()
            if reader.ident is not None:
                reader.join(timeout=2)
                if reader.is_alive():
                    raise RuntimeError("codemode worker output reader did not stop")
            stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _drive(
        self,
        stdin: Any,
        messages: queue.Queue[dict[str, Any]],
        invoke: ToolInvoker,
        state_store: Any,
        limits: CodeModeLimits,
        executor: ThreadPoolExecutor,
        stop: threading.Event,
        write_tools: set[str],
    ) -> CodeModeResult:
        pending: dict[str, Future[dict[str, Any]]] = {}
        child_index = 0
        while True:
            cancellation.check()
            self._flush_completed(pending, stdin)
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            kind = message.get("type")
            if kind == "call":
                if child_index >= limits.max_child_calls:
                    return CodeModeResult(False, error_code="call_limit", error="codemode child call limit exceeded",
                                          child_calls=child_index)
                call_id = str(message.get("callId", ""))
                if len(pending) >= limits.max_pending_promises:
                    self._write_line(stdin, {
                        "type": "result", "callId": call_id, "ok": False,
                        "errorCode": "pending_limit", "error": "codemode pending promise limit exceeded",
                    })
                else:
                    writing = message.get("name") in write_tools
                    if writing:
                        self._wait_callbacks(pending)
                    pending[call_id] = executor.submit(
                        self._handle_call, message, invoke, stop, cancellation.current_deadline(), child_index,
                    )
                    if writing:
                        self._wait_callbacks(pending)
                child_index += 1
                continue
            if kind == "state":
                if len(pending) >= limits.max_pending_promises:
                    return CodeModeResult(False, error_code="pending_limit", error="codemode pending promise limit exceeded",
                                          child_calls=child_index)
                response = self._handle_state(message, state_store)
                ready: Future[dict[str, Any]] = Future()
                ready.set_result(response)
                pending[str(message["callId"])] = ready
                continue
            if kind == "done":
                try:
                    value_bytes = len(json.dumps(message.get("value"), ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8"))
                except UnicodeEncodeError:
                    return CodeModeResult(False, error_code="result_encoding",
                                          error="codemode result contains invalid Unicode",
                                          child_calls=child_index)
                if message.get("ok") is True and value_bytes > limits.max_output_bytes:
                    return CodeModeResult(False, error_code="output_limit", error="codemode output exceeds its byte limit",
                                          child_calls=child_index)
                if message["ok"] is True and message["unawaitedCalls"]:
                    return CodeModeResult(
                        False, error_code="unawaited_calls",
                        error="codemode returned with unawaited calls; await every tool and state call",
                        child_calls=message["childCalls"],
                    )
                return CodeModeResult(
                    bool(message.get("ok")), value=message.get("value"),
                    output=str(message.get("output", "") or ""),
                    error_code=str(message.get("errorCode", "") or ""),
                    error=str(message.get("error", "") or ""),
                    child_calls=int(message.get("childCalls", 0) or 0),
                )
            if kind == "error":
                return CodeModeResult(False, error_code=str(message.get("code", "worker_error")), error=str(message.get("message", "worker error")))
            if kind == "eof":
                return CodeModeResult(False, error_code="worker_exit", error="codemode worker exited without a result")

    @staticmethod
    def _wait_callbacks(pending: dict[str, Future[dict[str, Any]]]) -> None:
        from concurrent.futures import wait

        unfinished = set(pending.values())
        while unfinished:
            cancellation.check()
            _, unfinished = wait(unfinished, timeout=0.05)

    @staticmethod
    def _handle_state(
        message: dict[str, Any], state_store: Any,
    ) -> dict[str, Any]:
        call_id = str(message.get("callId", ""))
        key = str(message.get("key", ""))
        operation = str(message.get("op", ""))
        if state_store is None:
            return {"type": "stateResult", "callId": call_id, "ok": False,
                    "errorCode": "state_unavailable", "error": "codemode state is unavailable"}
        try:
            if operation == "store":
                value = message.get("value")
                state_store.store(key, value)
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
        return response

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
                break
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
            # Node readline also splits on U+2028/U+2029. Keep the JSONL frame
            # ASCII so data separators cannot become transport delimiters.
            encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
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
        message: dict[str, Any], invoke: ToolInvoker, stop: threading.Event, deadline: float | None,
        child_index: int,
    ) -> dict[str, Any]:
        call_id = str(message.get("callId", ""))
        name = str(message.get("name", ""))
        args = message.get("args")
        if not isinstance(args, dict):
            return {"type": "result", "callId": call_id, "ok": False, "error": "tool args must be an object"}
        try:
            with cancellation.scope(stop), cancellation.deadline_scope(deadline):
                cancellation.check()
                token = rpc_child_index.set(child_index)
                try:
                    value = invoke(name, dict(args))
                finally:
                    rpc_child_index.reset(token)
            return {"type": "result", "callId": call_id, "ok": True, "value": value}
        except Exception as exc:
            return {"type": "result", "callId": call_id, "ok": False, "error": str(exc)[:400]}
