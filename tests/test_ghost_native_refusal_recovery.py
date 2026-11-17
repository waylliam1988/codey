"""Real HTTP, scoped Ghost revocation and durable native receipt continuation."""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.ghost.inbox import GhostInboxStore
from codey.operations.conversation_window import OpenWindow, WindowOpenReason, activate_window
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelObservationDeps, KernelResult
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectProjection, RuntimeEffectStore
from codey.runtime.effects.tool_result_delivery import DeliveryBatchProjection, ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject, decode_json_object
from codey.workspace.context_admission import GhostContextSource, request_scope
from tests.durable_runtime import accept_writer, open_runtime

SESSION = "native-receipt-session"
RUN = "native-receipt-run"
PROVIDER = "local"
HINT = "Prefer concise technical explanations."
CALL = "original-native-read"
POLICY = TaskPolicy(frozenset({"control", "project.read"}))


@dataclass
class Capture:
    endpoint: str
    payloads: list[JsonObject] = field(default_factory=list)


def _reply(protocol: str, *, initial: bool) -> JsonObject:
    name = "read_file" if initial else "done"
    arguments = '{"path":"a.py"}' if initial else '{"summary":"receipt reviewed"}'
    identity = CALL if initial else "final-done"
    if protocol == "openai-completions":
        return {"choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{
                "id": identity, "type": "function", "function": {"name": name, "arguments": arguments},
            }],
        }}]}
    return {"status": "completed", "output": [{
        "type": "function_call", "id": f"item-{identity}", "call_id": identity,
        "name": name, "arguments": arguments,
    }]}


@pytest.fixture()
def capture(protocol: str) -> Generator[Capture]:
    observed = Capture("")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            raw: object = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.payloads.append(decode_json_object(raw))
            body = json.dumps(_reply(protocol, initial=len(observed.payloads) == 1)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        observed.endpoint = f"http://127.0.0.1:{server.server_port}/v1"

        def serve() -> None:
            server.serve_forever(poll_interval=0.02)

        worker = Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield observed
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


class Runtime:
    def __init__(self, root: Path, capture: Capture, protocol: str, scope: str, ok: bool, mode: str) -> None:
        self.root = root
        self.capture = capture
        self.protocol = protocol
        self.scope = scope
        self.ok = ok
        self.mode = mode
        self.project = str(root) if scope == "project" else ""
        self.inbox = GhostInboxStore(root / "ghost")
        self.executed: list[ToolCall] = []
        self.durable, self.line = open_runtime(root, session_id=SESSION, run_id=RUN)
        accept_writer(self.durable, self.line, session_id=SESSION, run_id=RUN, project=self.project)
        self.outputs = ManagedOutputStore(root / "outputs")
        self.provider = self._provider()
        self.sink = self._sink()

    def _provider(self) -> ApiProvider:
        provider = ApiProvider(
            self.capture.endpoint, "deterministic-native-receipt-fixture", api_protocol=self.protocol,
            native_tools=True, timeout=5, temperature=0, context_window_tokens=262144,
            context_reserve_tokens=32768, context_keep_recent_tokens=32000, output_tokens=128,
        )
        provider.bind_context(SESSION)
        provider.bind_durable_context(self.durable, SESSION)
        return provider

    def _sink(self) -> KernelEffectSink:
        return KernelEffectSink(
            self.line, session_id=SESSION, run_id=RUN, provider_id=PROVIDER,
            durable=self.durable, managed_outputs=self.outputs,
        )

    def control(self, enabled: bool) -> None:
        assert self.inbox.set_control(
            "admit_hints", enabled, scope=self.scope, project=self.project, session_id=SESSION,
        )
        assert self.enabled() == enabled

    def enabled(self) -> bool:
        return self.inbox.control_enabled("admit_hints", project=self.project, session_id=SESSION)

    def read(self, call: ToolCall) -> ToolResult:
        self.executed.append(call)
        assert call.call_id == CALL and call.name == "read_file"
        if self.mode != "enabled":
            self.control(False)
        return ToolResult(call, "ORIGINAL_RECEIPT_OK" if self.ok else "ORIGINAL_RECEIPT_ERROR", ok=self.ok)

    def run(self) -> KernelResult:
        authority = request_scope(self.project, SESSION)
        source = GhostContextSource("ghost_directive", HINT, 900, "user" if self.scope == "user" else authority)
        request = KernelRunRequestBuilder(
            provider=self.provider, run_id=RUN, effect_scope="", provider_id=PROVIDER,
            user_task="Read a.py and report the result.", context_text=HINT, context_scope=authority,
            ghost_sources=(source,), attempt_admit_hints=self.enabled, window_ref=self.provider.window_ref,
        ).build(
            execution=KernelExecutionDeps(executors={"read_file": self.read}, intent_sink=self.sink, project_path=self.root),
            observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=self.durable, session_id=SESSION, task_id=RUN, sink=self.sink,
            )),
        )
        return run_task_kernel(TaskSession(POLICY, project=self.project, max_turns=4), request=request)

    def reopen(self) -> None:
        self.provider.close()
        self.durable.close()
        self.durable = DurableSession(SQLiteDurableStore(self.root / "durable.sqlite3"))
        self.durable.recover_scheduler_state(force=True)
        if self.durable.task(RUN).status == "pending":
            self.durable.claim_task(RUN)
        self.line = RuntimeMutationLine(durable=self.durable)
        self.line.mark_writer_running(SESSION, RUN, provider_id=PROVIDER)
        self.inbox = GhostInboxStore(self.root / "ghost")
        self.provider = self._provider()
        context = self.durable.context_snapshot(SESSION, token_budget=200000)
        assert any(CALL in json.dumps(message) for message in context.messages)
        activate_window(self.provider, OpenWindow(WindowOpenReason.FIRST_OPEN, context=context))
        self.sink = self._sink()

    def effects(self) -> tuple[RuntimeEffectProjection, ...]:
        return RuntimeEffectStore(durable=self.durable).load_effects(SESSION, RUN)

    def batches(self) -> tuple[DeliveryBatchProjection, ...]:
        return ToolResultDeliveryStore(durable=self.durable).load_batches(SESSION, RUN)

    def close(self) -> None:
        self.provider.close()
        self.durable.close()


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("ok", [True, False], ids=["ok", "error"])
@pytest.mark.parametrize("mode", ["enabled", "revoked_warm", "revoked_cold"])
def test_native_receipt_refusal_preserves_settlement_and_resumes_without_reexecution(
    tmp_path: Path, capture: Capture, protocol: str, scope: str, ok: bool, mode: str,
) -> None:
    runtime = Runtime(tmp_path, capture, protocol, scope, ok, mode)
    try:
        original_window = runtime.provider.window_ref
        assert original_window is not None
        first = runtime.run()
        if mode != "enabled":
            for attempt in range(2):
                refused = first if attempt == 0 else runtime.run()
                assert not refused.completed and refused.stop_reason == "provider_failure", refused
                assert "RequestPrepError" in refused.summary and "hints_disabled" in refused.summary, refused
                assert len(capture.payloads) == 1 and len(runtime.executed) == 1
                state = operation_from_checkpoint(runtime.durable.task(RUN), SESSION)
                assert state is not None
                assert state.leaf == "tool_delivery_pending" and state.logical_turn == 1
                failures = [row.settlement for row in runtime.effects() if row.intent.effect_category == "provider_send"
                            and row.settlement is not None and row.settlement.status == "error"]
                assert len(failures) == attempt + 1
                assert all(row.sent_state == "not_sent" and row.error_code == "RequestPrepError" for row in failures)
                [batch] = runtime.batches()
                assert not batch.is_delivered and len(batch.active_attempts) == 1
                if mode == "revoked_cold":
                    runtime.reopen()
                    assert not runtime.enabled()
            runtime.control(True)
            final = runtime.run()
        else:
            final = first
        assert final.completed and final.stop_reason == "done", final
        assert runtime.provider.window_ref == original_window
        assert len(runtime.executed) == 1 and len(capture.payloads) == 2
        outbound = json.dumps(capture.payloads[-1], ensure_ascii=False)
        assert CALL in outbound and HINT in outbound
        assert ("ORIGINAL_RECEIPT_OK" if ok else "ORIGINAL_RECEIPT_ERROR") in outbound
        read_effects = [row for row in runtime.effects() if row.intent.tool_name == "read"]
        assert len(read_effects) == 1
        receipt = read_effects[0].settlement
        assert receipt is not None and receipt.call_id == CALL
        assert receipt.status == ("ok" if ok else "error")
        assert ("ORIGINAL_RECEIPT_OK" if ok else "ORIGINAL_RECEIPT_ERROR") in receipt.result_text
        batches = runtime.batches()
        original = [batch for batch in batches if "read" in batch.intent.tool_names]
        assert len(original) == 1 and original[0].is_delivered
        assert len(original[0].superseded_effect_ids) == (0 if mode == "enabled" else 2)
        assert all(row.settlement is not None for row in runtime.effects())
    finally:
        runtime.close()
