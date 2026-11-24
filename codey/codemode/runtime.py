from __future__ import annotations

import hashlib
import json
import queue
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Literal, Protocol

from codey.codemode.catalog import build_catalog_snapshot, catalog_describe, catalog_search
from codey.codemode.identity import rpc_child_admission, rpc_child_allocator, rpc_child_index
from codey.codemode.protocol import CodeModeInputError, parse_codemode_args
from codey.codemode.state import CodeModeStateStore
from codey.codemode.types import CodeModeLimits, CodeModeProgram, CodeModeResult, DeliveryReference, ToolCatalogEntry
from codey.runtime.core import cancellation
from codey.types.json_value import decode_json_object, parse_json_object

ToolInvoker = Callable[[str, dict[str, object]], object]
WireMessage = dict[str, object]
DeliveryRecorder = Callable[[str, int | None], None]
SessionEventKind = Literal[
    "started", "child_started", "child_finished", "pending", "done", "error", "terminated",
]


@dataclass(frozen=True)
class CodeModeRequest:
    code: str
    catalog: tuple[ToolCatalogEntry, ...]
    invoke: ToolInvoker
    inputs: Mapping[str, object] = field(default_factory=dict)
    state_store: CodeModeStateStore | None = None
    replay_context: Mapping[str, object] | None = None
    delivery: tuple[DeliveryReference, ...] = ()
    on_delivery: DeliveryRecorder | None = None
    limits: CodeModeLimits | None = None
    execution_id: str = ""
    tool_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        identity = self.execution_id
        if not identity and self.replay_context is not None:
            replay_identity = self.replay_context.get("executionId")
            if isinstance(replay_identity, str):
                identity = replay_identity
        object.__setattr__(self, "execution_id", identity or "cme_" + secrets.token_hex(12))


@dataclass(frozen=True)
class CodeModeHandle:
    execution_id: str


@dataclass(frozen=True)
class CodeModeEvent:
    seq: int
    kind: SessionEventKind
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CodeModeOutcome:
    result: CodeModeResult | None
    events: tuple[CodeModeEvent, ...] = ()


class _SessionState:
    def __init__(self, handle: CodeModeHandle, request: CodeModeRequest) -> None:
        self.handle = handle
        self.request = request
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.condition = threading.Condition()
        self.events: list[CodeModeEvent] = []
        self.next_event = 0
        self.result: CodeModeResult | None = None
        self.failure: BaseException | None = None
        self.reason = ""

    def emit(self, kind: SessionEventKind, payload: Mapping[str, object] | None = None) -> None:
        with self.condition:
            self.events.append(CodeModeEvent(len(self.events), kind, dict(payload or {})))
            self.condition.notify_all()

    def admit_child(self) -> None:
        with self.condition:
            if self.stopping.is_set():
                raise RuntimeError("CodeMode execution is stopping; new child calls are closed")


class CodeModeSession:
    """One observable lifecycle over the existing QuickJS worker driver."""

    def __init__(
        self, runtime: NodeWorkerRuntime | None = None, *,
        restore: Callable[[str], CodeModeRequest | CodeModeOutcome] | None = None,
    ) -> None:
        self._runtime = runtime or NodeWorkerRuntime()
        # The owner reconstructs capabilities and receipts through the kernel.
        # Sessions never persist a second copy of requests or tool results.
        self._restore = restore
        self._states: dict[str, _SessionState] = {}
        self._lock = threading.Lock()

    def start(self, request: CodeModeRequest) -> CodeModeHandle:
        if not request.execution_id or type(request.tool_timeout_ms) is not int or request.tool_timeout_ms <= 0:
            raise ValueError("execution_id and a positive tool_timeout_ms are required")
        handle = CodeModeHandle(request.execution_id)
        state = _SessionState(handle, request)
        parent_stop, parent_deadline = cancellation.current_event(), cancellation.current_deadline()

        def run() -> None:
            try:
                with cancellation.scope(parent_stop), cancellation.deadline_scope(parent_deadline):
                    result = self._runtime._execute_request(request, state)
                with state.condition:
                    state.result = result
                kind: SessionEventKind = ("terminated" if result.error_code == "terminated"
                                          else "done" if result.ok else "error")
                state.emit(kind, {"ok": result.ok, "value": result.value, "error": result.error,
                                  "error_code": result.error_code, "child_calls": result.child_calls})
            except BaseException as exc:
                with state.condition:
                    state.failure = exc
                state.emit("error", {"error": str(exc)[:400]})
            finally:
                with state.condition:
                    state.finished.set()
                    state.condition.notify_all()

        thread = threading.Thread(target=run, name=f"codemode-{handle.execution_id}", daemon=True)
        with self._lock:
            if handle.execution_id in self._states:
                raise ValueError("CodeMode execution already started")
            state.emit("started", {"execution_id": handle.execution_id})
            thread.start()
            self._states[handle.execution_id] = state
        return handle

    def _state(self, handle: CodeModeHandle) -> _SessionState:
        with self._lock:
            return self._states[handle.execution_id]

    def wait(self, handle: CodeModeHandle, *, yield_ms: int) -> tuple[CodeModeEvent, ...]:
        if type(yield_ms) is not int or yield_ms <= 0:
            raise ValueError("yield_ms must be a positive integer")
        state = self._state(handle)
        with state.condition:
            changed = state.condition.wait_for(
                lambda: state.next_event < len(state.events) or state.finished.is_set(), yield_ms / 1000,
            )
            if not changed:
                state.emit("pending", {"execution_id": handle.execution_id})
            events = tuple(state.events[state.next_event:])
            state.next_event = len(state.events)
            return events

    def outcome(self, handle: CodeModeHandle) -> CodeModeOutcome:
        state = self._state(handle)
        with state.condition:
            if state.failure is not None:
                raise state.failure
            return CodeModeOutcome(state.result, tuple(state.events))

    def terminate(self, handle: CodeModeHandle, *, reason: str) -> CodeModeOutcome:
        if not reason.strip():
            raise ValueError("termination reason must be non-empty")
        state = self._state(handle)
        with state.condition:
            if not state.finished.is_set():
                state.reason = reason
                state.stopping.set()
        # A pending outcome retains the live handle. Only the worker owner can
        # publish terminated, after the admitted callbacks have settled.
        return self.outcome(handle)

    def recover(self, execution_id: str) -> CodeModeHandle | CodeModeOutcome:
        handle = CodeModeHandle(execution_id)
        with self._lock:
            state = self._states.get(execution_id)
        if state is None:
            if self._restore is None:
                raise KeyError(execution_id)
            restored = self._restore(execution_id)
            if isinstance(restored, CodeModeOutcome):
                if restored.result is None:
                    raise ValueError("restored outcome must be terminal")
                return restored
            if restored.execution_id != execution_id:
                raise ValueError("restored CodeMode execution identity changed")
            return self.start(restored)
        return self.outcome(handle) if state.finished.is_set() else handle


class _ChildCallAllocator:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._next = 0
        self._lock = threading.Lock()

    def reserve(self, count: int) -> tuple[int, ...]:
        with self._lock:
            if self._next + count > self._limit:
                raise ValueError("codemode child call limit exceeded")
            indices = tuple(range(self._next, self._next + count))
            self._next += count
            return indices

    @property
    def count(self) -> int:
        with self._lock:
            return self._next


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
        state_store: CodeModeStateStore | None = None,
        replay_context: Mapping[str, object] | None = None,
        delivery: Sequence[DeliveryReference] = (),
        on_delivery: DeliveryRecorder | None = None,
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
                    "engine": parse_json_object(probe.stdout), "rpc": 1, "determinism": 1,
                    "result_delivery": "completion_order_terminal_drain", "child_admission": "per_effect",
                    "heap_mb": 128, "timeout": "per_attempt"}
        return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def execute(
        self,
        code: str,
        *,
        catalog: Sequence[ToolCatalogEntry],
        invoke: ToolInvoker,
        inputs: Mapping[str, object] | None = None,
        state_store: CodeModeStateStore | None = None,
        replay_context: Mapping[str, object] | None = None,
        delivery: Sequence[DeliveryReference] = (),
        on_delivery: DeliveryRecorder | None = None,
        limits: CodeModeLimits | None = None,
    ) -> CodeModeResult:
        session = CodeModeSession(self)
        request = CodeModeRequest(code, tuple(catalog), invoke, {} if inputs is None else inputs,
                                  state_store, replay_context, tuple(delivery), on_delivery, limits)
        handle = session.start(request)
        while True:
            session.wait(handle, yield_ms=50)
            outcome = session.outcome(handle)
            if outcome.result is not None:
                return outcome.result

    def _execute_request(self, request: CodeModeRequest, control: _SessionState) -> CodeModeResult:
        cancellation.check()
        active_limits = request.limits or CodeModeLimits()
        try:
            program = parse_codemode_args({"code": request.code, "inputs": request.inputs}, limits=active_limits)
        except CodeModeInputError as exc:
            return CodeModeResult(False, error_code=exc.code, error=str(exc))
        with cancellation.linked_scope(deadline=time.monotonic() + active_limits.timeout_ms / 1000) as stop:
            try:
                return self._run_worker(program, request, active_limits, stop, control)
            except cancellation.DeadlineExceeded:
                return CodeModeResult(False, error_code="timeout", error="codemode execution timed out")

    def _run_worker(
        self, program: CodeModeProgram, execution: CodeModeRequest, limits: CodeModeLimits,
        stop: threading.Event, control: _SessionState,
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
        messages: queue.Queue[WireMessage] = queue.Queue(maxsize=limits.max_pending_promises + 8)
        completed: queue.Queue[str] = queue.Queue()

        def publish(message: WireMessage) -> None:
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
                    message = parse_json_object(line)
                except (ValueError, json.JSONDecodeError):
                    publish({"type": "error", "code": "protocol", "message": "worker emitted invalid JSON"})
                    continue
                publish({key: value for key, value in message.items()})
            publish({"type": "error", "code": "worker_exit", "message": "codemode worker exited without a result"})

        reader = threading.Thread(target=read_messages, daemon=True)
        request = {
            "type": "execute",
            "code": program.code,
            "inputs": self._plain_json(program.inputs),
            "catalog": [
                {"name": item.name, "description": item.description, "parameters": self._plain_json(item.parameters)}
                for item in execution.catalog
            ],
            "limits": {
                "timeoutMs": limits.timeout_ms,
                "maxChildCalls": limits.max_child_calls,
                "maxConcurrentCalls": limits.max_concurrent_calls,
                "maxPendingPromises": limits.max_pending_promises,
                "maxOutputBytes": limits.max_output_bytes,
            },
            "stateEnabled": execution.state_store is not None,
            "determinism": (dict(execution.replay_context) if execution.replay_context is not None
                            else {"epochMs": int(time.time() * 1000), "seed": 1}),
        }
        try:
            reader.start()
            # Host callbacks re-enter execute_turn; this pool owns no facts.
            with ThreadPoolExecutor(max_workers=limits.max_concurrent_calls) as executor:
                try:
                    self._write_line(stdin, request)
                    return self._drive(stdin, messages, completed, execution.invoke, execution.state_store,
                                       limits, executor, stop,
                                       {entry.name for entry in execution.catalog if not entry.read_only},
                                       execution.catalog, execution.delivery, execution.on_delivery, control)
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
        stdin: IO[str],
        messages: queue.Queue[WireMessage],
        completed: queue.Queue[str],
        invoke: ToolInvoker,
        state_store: CodeModeStateStore | None,
        limits: CodeModeLimits,
        executor: ThreadPoolExecutor,
        stop: threading.Event,
        write_tools: set[str],
        catalog: Sequence[ToolCatalogEntry],
        delivery: Sequence[DeliveryReference],
        on_delivery: DeliveryRecorder | None,
        control: _SessionState,
    ) -> CodeModeResult:
        pending: dict[str, Future[WireMessage]] = {}
        pending_indices: dict[str, int | None] = {}
        ready_ids: list[str] = []
        delivery_cursor = 0
        terminal: WireMessage | None = None
        allocator = _ChildCallAllocator(limits.max_child_calls)

        def mark_completed(_future: Future[WireMessage], *, call_id: str) -> None:
            completed.put(call_id)

        def completion_callback(call_id: str) -> Callable[[Future[WireMessage]], None]:
            def callback(done: Future[WireMessage]) -> None:
                mark_completed(done, call_id=call_id)
            return callback

        while True:
            cancellation.check()
            if control.stopping.is_set():
                self._wait_callbacks(pending)
                self._flush_completed(pending, pending_indices, completed, ready_ids, stdin, delivery,
                                      delivery_cursor, on_delivery)
                self._write_line(stdin, {"type": "terminate", "reason": control.reason})
                return CodeModeResult(False, error_code="terminated", error=control.reason,
                                      child_calls=allocator.count)
            try:
                delivery_cursor = self._flush_completed(
                    pending, pending_indices, completed, ready_ids, stdin, delivery, delivery_cursor, on_delivery,
                )
            except Exception as exc:
                return CodeModeResult(False, error_code="delivery_journal", error=str(exc),
                                      child_calls=allocator.count)
            if terminal is not None:
                if delivery_cursor < len(delivery) and delivery[delivery_cursor]["call_id"] not in pending:
                    return self._finish_message(terminal, allocator.count, limits, delivery_complete=False)
                if pending:
                    self._wait_callbacks(pending)
                    continue
                return self._finish_message(terminal, allocator.count, limits)
            try:
                message = messages.get(timeout=0.05)
            except queue.Empty:
                continue
            kind = message.get("type")
            if kind == "call":
                try:
                    child_index = allocator.reserve(1)[0]
                except ValueError:
                    return CodeModeResult(False, error_code="call_limit", error="codemode child call limit exceeded",
                                          child_calls=allocator.count)
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
                    future = executor.submit(
                        self._handle_call, message, invoke, stop,
                        min(cancellation.current_deadline() or float("inf"),
                            time.monotonic() + control.request.tool_timeout_ms / 1000),
                        child_index, allocator.reserve, control,
                    )
                    future.add_done_callback(completion_callback(call_id))
                    pending[call_id] = future
                    pending_indices[call_id] = child_index
                    if writing:
                        self._wait_callbacks(pending)
                continue
            if kind in {"state", "discover"}:
                failure = self._queue_immediate(
                    kind, message, pending, pending_indices, limits.max_pending_promises, state_store, catalog,
                    stdin, completion_callback,
                )
                if failure is not None:
                    return failure
                continue
            if kind == "done":
                terminal = message
                continue
            if kind == "error":
                return CodeModeResult(False, error_code=str(message.get("code", "worker_error")), error=str(message.get("message", "worker error")))

    @staticmethod
    def _finish_message(
        message: WireMessage, issued_count: int, limits: CodeModeLimits, *, delivery_complete: bool = True,
    ) -> CodeModeResult:
        if message.get("ok") is True and not delivery_complete:
            return CodeModeResult(False, error_code="delivery_journal",
                error="CodeMode returned before issuing its recorded result deliveries", child_calls=issued_count)
        try:
            value_bytes = len(json.dumps(message.get("value"), ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"))
        except UnicodeEncodeError:
            return CodeModeResult(False, error_code="result_encoding",
                                  error="codemode result contains invalid Unicode",
                                  child_calls=issued_count)
        if message.get("ok") is True and value_bytes > limits.max_output_bytes:
            return CodeModeResult(False, error_code="output_limit", error="codemode output exceeds its byte limit",
                                  child_calls=issued_count)
        raw_child_calls = message.get("childCalls", 0)
        child_calls = raw_child_calls if type(raw_child_calls) is int else issued_count
        if message["ok"] is True and message["unawaitedCalls"]:
            return CodeModeResult(
                False, error_code="unawaited_calls",
                error="codemode returned with unawaited calls; await every tool and state call",
                child_calls=child_calls,
            )
        return CodeModeResult(
            bool(message.get("ok")), value=message.get("value"),
            output=str(message.get("output", "") or ""),
            error_code=str(message.get("errorCode", "") or ""),
            error=str(message.get("error", "") or ""),
            child_calls=max(issued_count, child_calls),
        )

    @staticmethod
    def _wait_callbacks(pending: dict[str, Future[WireMessage]]) -> None:
        from concurrent.futures import wait

        unfinished = set(pending.values())
        while unfinished:
            cancellation.check()
            _, unfinished = wait(unfinished, timeout=0.05)

    @staticmethod
    def _queue_immediate(
        kind: object, message: WireMessage, pending: dict[str, Future[WireMessage]],
        pending_indices: dict[str, int | None], pending_limit: int, state_store: CodeModeStateStore | None,
        catalog: Sequence[ToolCatalogEntry], stdin: IO[str],
        completion_callback: Callable[[str], Callable[[Future[WireMessage]], None]],
    ) -> CodeModeResult | None:
        if len(pending) >= pending_limit:
            if kind == "discover":
                NodeWorkerRuntime._write_line(stdin, {
                    "type": "result", "callId": str(message.get("callId", "")), "ok": False,
                    "errorCode": "pending_limit", "error": "codemode pending promise limit exceeded",
                })
                return None
            return CodeModeResult(False, error_code="pending_limit", error="codemode pending promise limit exceeded")
        response = (NodeWorkerRuntime._handle_state(message, state_store)
                    if kind == "state" else NodeWorkerRuntime._handle_discovery(message, catalog))
        call_id = str(message.get("callId", ""))
        ready_future: Future[WireMessage] = Future()
        ready_future.set_result(response)
        ready_future.add_done_callback(completion_callback(call_id))
        pending[call_id] = ready_future
        pending_indices[call_id] = None
        return None

    @staticmethod
    def _handle_state(
        message: WireMessage, state_store: CodeModeStateStore | None,
    ) -> WireMessage:
        call_id = str(message.get("callId", ""))
        key = str(message.get("key", ""))
        operation = str(message.get("op", ""))
        response: WireMessage
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
    def _handle_discovery(message: WireMessage, catalog: Sequence[ToolCatalogEntry]) -> WireMessage:
        snapshot = build_catalog_snapshot(catalog)
        call_id = str(message.get("callId", ""))
        operation = str(message.get("op", ""))
        if operation == "describe":
            path = message.get("path")
            if not isinstance(path, str):
                return {"type": "result", "callId": call_id, "ok": False,
                        "errorCode": "argument_encoding", "error": "describe path must be a string"}
            item = catalog_describe(snapshot, path)
            value: object = None if item is None else {
                "path": item.path, "signature": item.signature, "description": item.description,
            }
            return {"type": "result", "callId": call_id, "ok": True, "value": value}
        if operation != "search":
            return {"type": "result", "callId": call_id, "ok": False,
                    "errorCode": "discovery_operation", "error": "unknown catalog discovery operation"}
        query = message.get("query", "")
        options = message.get("options", {})
        if not isinstance(query, str) or not isinstance(options, dict):
            return {"type": "result", "callId": call_id, "ok": False,
                    "errorCode": "argument_encoding", "error": "search arguments are invalid"}
        namespace = options.get("namespace")
        limit = options.get("limit", 10)
        offset = options.get("offset", 0)
        if namespace is not None and not isinstance(namespace, str):
            return {"type": "result", "callId": call_id, "ok": False,
                    "errorCode": "argument_encoding", "error": "search namespace must be a string"}
        if type(limit) is not int or type(offset) is not int:
            return {"type": "result", "callId": call_id, "ok": False,
                    "errorCode": "argument_encoding", "error": "search limit and offset must be integers"}
        try:
            result = catalog_search(snapshot, query, namespace=namespace, limit=limit, offset=offset)
        except ValueError as exc:
            return {"type": "result", "callId": call_id, "ok": False,
                    "errorCode": "argument_encoding", "error": str(exc)}
        return {"type": "result", "callId": call_id, "ok": True,
                "value": {"items": [{"path": item.path, "signature": item.signature, "description": item.description}
                                     for item in result.items],
                          "remaining": result.remaining, "next_offset": result.next_offset}}

    @staticmethod
    def _plain_json(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): NodeWorkerRuntime._plain_json(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [NodeWorkerRuntime._plain_json(item) for item in value]
        return value

    @staticmethod
    def _flush_completed(
        pending: dict[str, Future[WireMessage]], pending_indices: dict[str, int | None], completed: queue.Queue[str],
        ready_ids: list[str], stdin: IO[str], delivery: Sequence[DeliveryReference], delivery_cursor: int,
        on_delivery: DeliveryRecorder | None,
    ) -> int:
        while True:
            try:
                call_id = completed.get_nowait()
                if call_id not in ready_ids:
                    ready_ids.append(call_id)
            except queue.Empty:
                break
        while ready_ids:
            expected = delivery[delivery_cursor] if delivery_cursor < len(delivery) else None
            expected_id = expected["call_id"] if expected is not None else ""
            if expected_id and expected_id not in ready_ids:
                break
            call_id = expected_id or ready_ids[0]
            ready_ids.remove(call_id)
            future = pending.pop(call_id, None)
            child_index = pending_indices.pop(call_id)
            if expected is not None and expected["child_index"] != child_index:
                raise ValueError("CodeMode result delivery changed its child identity")
            if future is None:
                continue
            response: WireMessage
            try:
                response = future.result()
            except Exception as exc:
                response = {
                    "type": "result", "callId": call_id, "ok": False,
                    "errorCode": "host_execution_error", "error": str(exc)[:400],
                }
            if on_delivery is not None:
                on_delivery(call_id, child_index)
            NodeWorkerRuntime._write_line(stdin, response, call_id=call_id)
            if expected_id == call_id:
                delivery_cursor += 1
        return delivery_cursor

    @staticmethod
    def _write_line(stdin: IO[str], payload: WireMessage, *, call_id: str = "") -> None:
        try:
            # Node readline also splits on U+2028/U+2029. Keep the JSONL frame
            # ASCII so data separators cannot become transport delimiters.
            encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            encoded = json.dumps(dict[str, object]({
                "type": "result", "callId": call_id, "ok": False,
                "errorCode": "host_result_encoding",
                "error": "host tool result is not JSON serializable",
            }))
        stdin.write(encoded + "\n")
        stdin.flush()

    @staticmethod
    def _handle_call(
        message: WireMessage, invoke: ToolInvoker, stop: threading.Event, deadline: float | None,
        child_index: int, allocate_child: Callable[[int], tuple[int, ...]],
        control: _SessionState,
    ) -> WireMessage:
        call_id = str(message.get("callId", ""))
        name = str(message.get("name", ""))
        args = message.get("args")
        if not isinstance(args, dict):
            return {"type": "result", "callId": call_id, "ok": False, "error": "tool args must be an object"}
        started = False
        try:
            with cancellation.scope(stop), cancellation.deadline_scope(deadline):
                cancellation.check()
                with control.condition:
                    control.admit_child()
                    control.emit("child_started", {"call_id": call_id, "child_index": child_index, "name": name})
                    started = True
                index_token = rpc_child_index.set(child_index)
                allocator_token = rpc_child_allocator.set(allocate_child)
                admission_token = rpc_child_admission.set(control.admit_child)
                try:
                    value = invoke(name, {key: item for key, item in decode_json_object(message["args"]).items()})
                finally:
                    rpc_child_admission.reset(admission_token)
                    rpc_child_allocator.reset(allocator_token)
                    rpc_child_index.reset(index_token)
            return {"type": "result", "callId": call_id, "ok": True, "value": value}
        except Exception as exc:
            return {"type": "result", "callId": call_id, "ok": False, "error": str(exc)[:400]}
        finally:
            if started:
                control.emit("child_finished", {"call_id": call_id, "child_index": child_index, "name": name})
