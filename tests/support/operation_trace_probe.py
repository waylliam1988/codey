"""Deterministic provider traces through the real operation/tool write path.

This probe supplies provider responses and external tool observations only.
It does not choose runtime actions or implement a recovery loop. The same
probe measures the pre-migration commit and the final operation driver.
"""
from __future__ import annotations

import json
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from codey.agents.shell_approval import ShellApprovalRequest
from codey.durable import ApprovalRecord, ApprovalStatus, DurableSession
from codey.durable.model import thaw_json
from codey.operations.generation_boundary import (
    GenerationBoundaryRequest,
    GenerationBoundaryResult,
    durable_generation_boundary_callback,
)
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult, TurnFinish
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.observe.events import RunEvent
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject
from tests.support.sqlite_store import TemporarySQLiteStore


class ProcessInterrupted(BaseException):
    """Bypass controller exception mapping at an observed commit boundary."""


@dataclass(frozen=True)
class Step:
    name: str
    args: JsonObject
    finish: TurnFinish = TurnFinish.COMPLETE


READ = Step("read_file", {"path": "a.py"})
DONE = Step("done", {"summary": "finished"})
BAD = Step("unknown_tool", {})
LENGTH = Step("", {}, TurnFinish.OUTPUT_LIMIT)
QUESTION = Step("ask_user", {"question": "Choose a mode", "options": ["A", "B"]})
SHELL = Step("shell", {"command": "echo trace-approval", "path": "."})


class TraceProvider:
    name = "scripted"

    def __init__(self, steps: list[Step], *, native: bool, trace: list[dict[str, object]],
                 stop: Event, cancel_after_reply: bool = False, close_steps: int = 0) -> None:
        self.native_tools = native
        self.steps = iter(enumerate(steps, start=1))
        self.trace = trace
        self.stop = stop
        self.cancel_after_reply = cancel_after_reply
        self.close_steps = close_steps

    def _next(self, method: str, tools: Sequence[ProviderToolDefinition], receipts: Sequence[ProviderToolResult] = ()) -> AssistantTurn | str:
        index, step = next(self.steps)  # Exhaustion is a probe failure, never synthetic done.
        self.trace.append({"event": method, "reply": step.name or step.finish.value,
                           "tools": [tool.name for tool in tools],
                           "call_ids": [row.call_id for row in receipts]})
        if self.cancel_after_reply:
            self.stop.set()
        if self.native_tools:
            return AssistantTurn(
                text="partial" if step.finish is TurnFinish.OUTPUT_LIMIT else "",
                tool_calls=(ProviderToolCall(f"call-{index}", step.name, step.args),) if step.name else (),
                finish=step.finish,
            )
        return json.dumps({"tool": step.name, "args": step.args})

    def send(self, prompt: str) -> str:
        result = self._next("send", ())
        assert isinstance(result, str)
        return result

    def send_turn(self, prompt: str, tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        result = self._next("send_turn", tools)
        assert isinstance(result, AssistantTurn)
        return result

    def send_tool_results(self, results: Sequence[ProviderToolResult], tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        result = self._next("send_tool_results", tools, results)
        assert isinstance(result, AssistantTurn)
        return result

    def acknowledge_tool_results(self, results: Sequence[ProviderToolResult], declared_tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        self.trace.append({"event": "acknowledge", "call_ids": [row.call_id for row in results],
                           "errors": [row.content.startswith("ERROR:") for row in results]})
        if self.close_steps:
            self.close_steps -= 1
            return AssistantTurn(tool_calls=(ProviderToolCall(
                f"closing-{self.close_steps}", "read_file", {"path": "a.py"}),))
        return AssistantTurn()


def observe_trace(
    project: Path, steps: list[Step], *, native: bool = False, max_turns: int = 4,
    changes_required: bool = False, cancel_after_reply: bool = False,
    cancel_after_tool: bool = False, close_steps: int = 0, interrupt_at: str = "",
    shell_approval: bool = False, stagnant_turns: int | None = None,
    durable: DurableSession | None = None,
) -> dict[str, object]:
    """Return semantic observations plus measured commits/size/time.

    Faults are raised only at real provider, effect and boundary observations.
    A supplied SQLite session lets a child-process harness kill the worker at
    the same boundaries; this unit probe uses an isolated production database.
    """
    owned = durable is None
    if durable is None:
        durable = DurableSession(TemporarySQLiteStore())
    trace: list[dict[str, object]] = []
    stop = Event()
    durable.admit_input(conversation_id="trace-session", request_id="trace-request", task_id="trace-run",
                        text="inspect a.py", provider_snapshot={"provider_id": "scripted"})
    durable.claim_task("trace-run")
    durable.start_task("trace-run", state=dict(durable.task("trace-run").state))
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="trace-session", run_id="trace-run", provider_id="scripted",
                          turn_budget=max_turns, max_repair_rounds=1, task_kind="project")
    line.mark_writer_running("trace-session", "trace-run", provider_id="scripted")
    sink = KernelEffectSink(line, session_id="trace-session", run_id="trace-run", provider_id="scripted",
                            durable=durable,
                            managed_outputs=ManagedOutputStore(project / ".state"))
    provider = TraceProvider(steps, native=native, trace=trace, stop=stop,
                             cancel_after_reply=cancel_after_reply, close_steps=close_steps)
    grants = {"control", "project.read"}
    if shell_approval:
        grants.add("shell.approval")
    session = TaskSession(TaskPolicy(frozenset(grants)), project=str(project),
                          max_turns=max_turns, project_changes_required=changes_required)
    boundary = durable_generation_boundary_callback(
        durable=durable, session_id="trace-session", task_id="trace-run", sink=sink)

    def checkpoint(name: str) -> None:
        if interrupt_at == name:
            raise ProcessInterrupted(name)

    def execute(call: ToolCall) -> ToolResult:
        trace.append({"event": "execute", "name": call.name, "call_id": call.call_id})
        checkpoint("tool_started")
        if cancel_after_tool:
            stop.set()
        return ToolResult(call, "a.py contents", ok=True)

    def event(value: RunEvent) -> None:
        if value.kind == "turn":
            checkpoint("response_returned")
        if value.kind == "user_question":
            trace.append({"event": "question", "id": value.metadata["id"]})

    def at_boundary(request: GenerationBoundaryRequest) -> GenerationBoundaryResult:
        kind = request.kind
        trace.append({"event": "boundary", "kind": kind})
        checkpoint(f"before_{kind}")
        result = boundary(request)
        checkpoint(f"after_{kind}")
        return result

    def approval(value: ShellApprovalRequest) -> None:
        from codey.providers.response_record import json_value

        ref = sink.managed_outputs.write_json_artifact(session_id="trace-session", run_id="trace-run",
                                                       payload=json_value(value))
        durable.create_approval(ApprovalRecord("trace-approval", "trace-run", "trace-session",
                                               ApprovalStatus.REQUESTED, ref.sha256, ref.handle, value.cwd))
        trace.append({"event": "approval", "call_id": value.call_id, "turn": value.turn,
                      "tool_index": value.tool_index, "command": value.command})

    started = time.perf_counter()
    before = durable.commit_sequence()
    result = None
    with suppress(ProcessInterrupted):
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=adapt_provider(provider), run_id="trace-run",
                provider_id="scripted", user_task="inspect a.py", stop_flag=stop, stagnant_turns=stagnant_turns),
            execution=KernelExecutionDeps(executors={"read_file": execute}, project_path=project),
            observation=KernelObservationDeps(intent_sink=sink, on_event=event, on_boundary=at_boundary,
                on_shell_request=approval if shell_approval else None),
        ))
    elapsed = time.perf_counter() - started
    effects = durable.effects("trace-run")
    entries = durable.entries("trace-session")
    report = {
        "trace": trace,
        "stop_reason": result.stop_reason if result is not None else "interrupted",
        "turns": result.turns if result is not None else session.turn,
        "completed": result.completed if result is not None else False,
        "provider_calls": sum(effect.category == "provider_send" for effect in effects),
        "tool_executions": sum(row["event"] == "execute" for row in trace),
        "tool_receipts": sum(effect.category == "tool_call" and effect.settlement is not None for effect in effects),
        "pending_effects": [effect.category for effect in effects if effect.settlement is None],
        "history_kinds": [entry.kind for entry in entries],
        "metrics": {"commits": durable.commit_sequence() - before, "elapsed_seconds": elapsed,
                    "checkpoint_bytes": len(json.dumps(thaw_json(durable.task("trace-run").state)).encode())},
    }
    if owned:
        durable.close()
    return report
