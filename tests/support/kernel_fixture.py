"""Typed fixtures around the production durable kernel and project adapter."""
from __future__ import annotations

import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from codey.agents.request import AgentRequest
from codey.agents.shell_approval import ShellApprovalRequest
from codey.durable import ApprovalRecord, ApprovalStatus, DurableSession
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import KernelResult, KernelRunRequest
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.providers.response_record import json_value
from codey.runtime.core.run_result import RunResult
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.sqlite_store import TemporarySQLiteStore


def approval_notifier(durable: DurableSession, outputs: ManagedOutputStore, session_id: str, run_id: str,
                      callback: Callable[[ShellApprovalRequest], None] | None) -> Callable[[ShellApprovalRequest], None] | None:
    if callback is None:
        return None

    def notify(request: ShellApprovalRequest) -> None:
        artifact = outputs.write_json_artifact(session_id=session_id, run_id=run_id, payload=json_value(request))
        durable.create_approval(ApprovalRecord(f'approval-{run_id}-{request.turn}-{request.tool_index}', run_id,
            session_id, ApprovalStatus.REQUESTED, artifact.sha256, artifact.handle, request.cwd))
        callback(request)

    return notify


def admit(durable: DurableSession, *, run_id: str, session_id: str, task: str, provider_id: str,
          max_turns: int, task_kind: str = 'project') -> RuntimeMutationLine:
    try:
        checkpoint = durable.task(run_id)
    except KeyError:
        durable.admit_input(conversation_id=session_id, request_id=run_id, task_id=run_id,
                            text=task, provider_snapshot={})
        checkpoint = durable.task(run_id)
    if checkpoint.status == 'pending':
        durable.claim_task(run_id)
    durable.start_task(run_id, state=dict(durable.task(run_id).state))
    mutations = RuntimeMutationLine(durable=durable)
    if operation_from_checkpoint(durable.task(run_id), session_id) is None:
        mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id=provider_id,
            turn_budget=max_turns, max_repair_rounds=1, task_kind=task_kind)
        mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
    return mutations


@contextmanager
def runtime(*, session_id: str = 'test-session', run_id: str = 'test-run', task: str = 'test task',
            provider_id: str = 'test', max_turns: int = 4, task_kind: str = 'project') -> Generator[KernelEffectSink, None, None]:
    with tempfile.TemporaryDirectory() as temporary:
        durable = DurableSession(TemporarySQLiteStore())
        try:
            mutations = admit(durable, run_id=run_id, session_id=session_id, task=task, provider_id=provider_id,
                              max_turns=max_turns, task_kind=task_kind)
            yield KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id=provider_id,
                                   durable=durable, managed_outputs=ManagedOutputStore(Path(temporary)))
        finally:
            durable.close()


def run_task_kernel(session: TaskSession, *, request: KernelRunRequest, session_id: str = 'test-session') -> KernelResult:
    from codey.operations.task_loop import run_task_kernel as run

    sink = request.execution.intent_sink
    if sink is not None:
        if request.observation.on_boundary is None:
            request = replace(request, observation=replace(request.observation,
                on_boundary=durable_generation_boundary_callback(durable=sink.durable,
                    session_id=sink.session_id, task_id=sink.run_id, sink=sink)))
        return run(session, request=request)
    with runtime(session_id=session_id, run_id=request.transport.run_id or 'test-run',
                 task=request.transport.user_task or 'test task', provider_id=request.transport.provider_id or 'test',
                 max_turns=session.max_turns, task_kind=session.task_kind) as actual:
        request = replace(request, transport=replace(request.transport, run_id=actual.run_id),
            execution=replace(request.execution, intent_sink=actual), observation=replace(request.observation,
                on_boundary=durable_generation_boundary_callback(durable=actual.durable,
                    session_id=actual.session_id, task_id=actual.run_id, sink=actual),
                on_shell_request=approval_notifier(actual.durable, actual.managed_outputs,
                    actual.session_id, actual.run_id, request.observation.on_shell_request)))
        return run(session, request=request)


def run_agent(request: AgentRequest) -> RunResult:
    from codey.operations.project_adapter import run

    with tempfile.TemporaryDirectory() as temporary:
        owned = request.durable is None
        durable = request.durable or DurableSession(TemporarySQLiteStore())
        session_id, run_id = request.session_id or 'test-session', request.run_id or 'test-run'
        provider_id = request.provider_id or 'test'
        try:
            mutations = request.runtime_mutations or admit(durable, run_id=run_id, session_id=session_id,
                task=request.task, provider_id=provider_id, max_turns=request.max_turns)
            outputs = request.managed_outputs or ManagedOutputStore(Path(temporary))
            return run(replace(request, durable=durable, runtime_mutations=mutations,
                session_id=session_id, run_id=run_id, provider_id=provider_id, managed_outputs=outputs,
                on_shell_request=approval_notifier(durable, outputs, session_id, run_id, request.on_shell_request)))
        finally:
            if owned:
                durable.close()
