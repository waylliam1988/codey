"""Explicit memory durable runtime for kernel unit tests.

Production rejects missing durable dependencies. Tests requesting an isolated
runtime use the same operation driver, transaction writer and receipt store.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelResult,
    KernelRunRequest,
    KernelTransportDeps,
)

__all__ = ["KernelExecutionDeps", "KernelObservationDeps", "KernelResult", "KernelRunRequest", "KernelTransportDeps",
           "run_task_kernel"]


def approval_notifier(durable, outputs, session_id, run_id, callback):
    """Publish the actual approval fact before notifying an isolated test UI."""
    if callback is None:
        return None
    from codey.durable import ApprovalRecord, ApprovalStatus
    from codey.providers.response_record import json_value

    def notify(request):
        command = outputs.write_json_artifact(session_id=session_id, run_id=run_id,
                                             payload=json_value(request))
        durable.create_approval(ApprovalRecord(f"approval-{run_id}-{request.turn}-{request.tool_index}", run_id, session_id,
            ApprovalStatus.REQUESTED, command.sha256, command.handle, str(request.cwd)))
        callback(request)
    return notify


def run_task_kernel(session, *, request):
    from codey.durable import DurableSession, MemoryDurableStore
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_loop import run_task_kernel as run
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    sink = request.observation.intent_sink
    if isinstance(sink, KernelEffectSink):
        if request.observation.on_boundary is None:
            request = replace(request, observation=replace(request.observation,
                on_boundary=durable_generation_boundary_callback(durable=sink.durable,
                    session_id=sink.session_id, task_id=sink.run_id, sink=sink)))
        return run(session, request=request)
    with tempfile.TemporaryDirectory() as temporary:
        durable = DurableSession(MemoryDurableStore())
        run_id = request.transport.run_id or "test-run"
        session_id = request.execution.session_id or "test-session"
        durable.admit_input(conversation_id=session_id, request_id="test-input", task_id=run_id,
                            text=str(request.transport.user_task or "test task"), provider_snapshot={})
        durable.claim_task(run_id)
        durable.start_task(run_id, state=dict(durable.task(run_id).state))
        mutations = RuntimeMutationLine(durable=durable)
        provider_id = str(request.transport.provider_id or "test")
        mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id=provider_id,
            turn_budget=session.max_turns, max_repair_rounds=1, task_kind=session.task_kind)
        mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
        outputs = request.execution.managed_outputs or ManagedOutputStore(Path(temporary))
        actual_sink = KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id=provider_id,
                                      durable=durable, managed_outputs=outputs)
        if sink is not None:
            for method in ("begin_turn", "settle"):
                observer = getattr(sink, method, None)
                if observer is not None:
                    original = getattr(actual_sink, method)
                    def observed(*args, observer=observer, original=original, **kwargs):
                        observer(*args, **kwargs)
                        return original(*args, **kwargs)
                    setattr(actual_sink, method, observed)
        boundary = durable_generation_boundary_callback(durable=durable, session_id=session_id, task_id=run_id, sink=actual_sink)
        callback = request.observation.on_boundary

        def on_boundary(boundary_request):
            if callback is not None:
                callback(boundary_request.kind)
            return boundary(boundary_request)

        request = replace(request,
            transport=replace(request.transport, run_id=run_id),
            execution=replace(request.execution, durable=durable, managed_outputs=outputs, session_id=session_id),
            observation=replace(request.observation, intent_sink=actual_sink, on_boundary=on_boundary,
                on_shell_request=approval_notifier(durable, outputs, session_id, run_id, request.observation.on_shell_request)))
        try:
            return run(session, request=request)
        finally:
            durable.close()


def run_agent(request):
    """Explicitly provision durable dependencies for isolated adapter tests."""
    from codey.durable import DurableSession, MemoryDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.project_adapter import run
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    with tempfile.TemporaryDirectory() as temporary:
        owned = request.durable is None
        durable = DurableSession(MemoryDurableStore()) if owned else request.durable
        run_id, session_id = request.run_id or "test-run", request.session_id or "test-session"
        provider_id = request.provider_id or "test" if request.runtime_mutations is None else request.provider_id
        try:
            try:
                task = durable.task(run_id)
            except KeyError:
                durable.admit_input(conversation_id=session_id, request_id=run_id, task_id=run_id,
                    text=request.task, provider_snapshot={})
                task = durable.task(run_id)
            if task.status == "pending":
                durable.claim_task(run_id)
            durable.start_task(run_id, state=dict(durable.task(run_id).state))
            mutations = request.runtime_mutations or RuntimeMutationLine(durable=durable)
            if operation_from_checkpoint(durable.task(run_id), session_id) is None:
                mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id=provider_id,
                    turn_budget=request.max_turns, max_repair_rounds=1)
                mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
            outputs = request.managed_outputs or ManagedOutputStore(Path(temporary))
            return run(replace(request, run_id=run_id, session_id=session_id, provider_id=provider_id,
                durable=durable, runtime_mutations=mutations,
                managed_outputs=outputs,
                on_shell_request=approval_notifier(durable, outputs, session_id, run_id, request.on_shell_request)))
        finally:
            if owned:
                durable.close()


def run_entry_kernel(frame, work, hooks, deps, **kwargs):
    """Provision fixtures explicitly, then enter the production operation."""
    from types import SimpleNamespace

    from codey.durable import DurableSession, MemoryDurableStore
    from codey.durable.checkpoint import operation_from_checkpoint
    from codey.operations.task_entry import run_entry_kernel as run
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    with tempfile.TemporaryDirectory() as temporary:
        state = deps.state
        owned = getattr(state, "durable", None) is None
        durable = DurableSession(MemoryDurableStore()) if owned else state.durable
        if owned:
            state.durable = durable
        outputs = getattr(deps, "managed_outputs", None) or getattr(state, "managed_outputs", None) or ManagedOutputStore(Path(temporary))
        mutations = getattr(deps, "runtime_mutations", None) or RuntimeMutationLine(durable=durable)
        try:
            try:
                task = durable.task(frame.run_id)
            except KeyError:
                durable.admit_input(conversation_id=frame.request.session_id, request_id=frame.run_id,
                    task_id=frame.run_id, text=frame.request.task, provider_snapshot={})
                durable.claim_task(frame.run_id)
                task = durable.task(frame.run_id)
            if task.phase != "running":
                durable.start_task(frame.run_id, state=dict(task.state))
            if operation_from_checkpoint(durable.task(frame.run_id), frame.request.session_id) is None:
                mutations.accept_operation(session_id=frame.request.session_id, run_id=frame.run_id,
                    provider_id=frame.provider_id, turn_budget=frame.request.max_turns, max_repair_rounds=1)
            resources = SimpleNamespace(**{**vars(deps), "runtime_mutations": mutations, "managed_outputs":outputs})
            return run(frame, work, hooks, resources, **kwargs)
        finally:
            if owned:
                durable.close()


def run_research_iteration(deps, **kwargs):
    """Provision explicit isolated resources for research probes."""
    from types import SimpleNamespace

    from codey.durable import DurableSession, MemoryDurableStore
    from codey.operations.research_iteration import run_research_iteration as run
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    if getattr(deps.state, "durable", None) is not None:
        return run(deps, **kwargs)
    with tempfile.TemporaryDirectory() as temporary:
        durable = DurableSession(MemoryDurableStore())
        run_id = kwargs["run_id"] or "research-run"
        session_id = kwargs["session_id"] or "research-session"
        kwargs.update(run_id=run_id, session_id=session_id)
        mutations = RuntimeMutationLine(durable=durable)
        durable.admit_input(conversation_id=session_id, request_id=run_id, task_id=run_id,
                            text=kwargs["task"], provider_snapshot={})
        durable.claim_task(run_id)
        durable.start_task(run_id, state=dict(durable.task(run_id).state))
        mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id=kwargs["provider_id"],
            turn_budget=kwargs["max_turns"], max_repair_rounds=0, task_kind="research")
        state = SimpleNamespace(**{**vars(deps.state), "durable": durable})
        resources = SimpleNamespace(state=state, knowledge_store=deps.knowledge_store,
            managed_outputs=getattr(deps, "managed_outputs", None) or ManagedOutputStore(Path(temporary)), runtime_mutations=mutations)
        try:
            return run(resources, **kwargs)
        finally:
            durable.close()


def run_evidence_followup(**kwargs):
    """An evidence-only fixture starts after a committed parent invocation."""
    from types import SimpleNamespace

    from codey.durable import DurableSession, MemoryDurableStore
    from codey.operations.evidence_followup import run_evidence_followup as run
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    with tempfile.TemporaryDirectory() as temporary:
        durable = DurableSession(MemoryDurableStore())
        session_id, run_id, provider_id = kwargs.get("session_id") or "s", kwargs.get("run_id") or "r", kwargs.get("provider_id") or "local"
        mutations = RuntimeMutationLine(durable=durable)
        outputs = kwargs.get("managed_outputs") or ManagedOutputStore(Path(temporary))
        durable.admit_input(conversation_id=session_id, request_id=run_id, task_id=run_id,
                            text=kwargs["question"], provider_snapshot={})
        durable.claim_task(run_id)
        durable.start_task(run_id, state=dict(durable.task(run_id).state))
        mutations.accept_operation(session_id=session_id, run_id=run_id, provider_id=provider_id,
                                   turn_budget=8, max_repair_rounds=0, task_kind="research")
        mutations.mark_writer_running(session_id, run_id, provider_id=provider_id)
        sink = KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id=provider_id,
                                durable=durable, managed_outputs=outputs)
        try:
            from codey.operations.kernel_driver import drive_kernel

            drive_kernel(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat"), request=KernelRunRequest(
                transport=KernelTransportDeps(provider=SimpleNamespace(send=lambda *_: '{"tool":"done","args":{"summary":"parent finished"}}'), run_id=run_id, provider_id=provider_id),
                observation=KernelObservationDeps(intent_sink=sink, on_boundary=durable_generation_boundary_callback(
                    durable=durable, session_id=session_id, task_id=run_id, sink=sink))))
            return run(**{**kwargs, "session_id":session_id, "run_id":run_id, "provider_id":provider_id,
                "runtime_mutations":mutations, "durable":durable, "managed_outputs":outputs})
        finally:
            durable.close()


def run_project_audit_advisor(*args, **kwargs):
    """Explicit store resources for isolated read-only advisor tests."""
    from codey.durable import DurableSession, MemoryDurableStore
    from codey.operations.project_audit_advisor import run_project_audit_advisor as run
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    with tempfile.TemporaryDirectory() as temporary:
        durable = DurableSession(MemoryDurableStore())
        try:
            return run(*args, **kwargs, durable=durable, runtime_mutations=RuntimeMutationLine(durable=durable),
                       managed_outputs=ManagedOutputStore(Path(temporary)))
        finally:
            durable.close()
