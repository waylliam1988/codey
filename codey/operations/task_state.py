"""State and services consumed by the task operation spine."""
from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from codey.agents.handoff import ConversationContext
    from codey.durable.session import DurableSession
    from codey.knowledge.store import KnowledgeStore
    from codey.memory.service import MemoryService
    from codey.providers.base import ChatProvider
    from codey.providers.diagnostics import ProviderFailure
    from codey.providers.supervisor import ProviderSupervisor
    from codey.repairs.self_repair import SelfRepairSupervisor
    from codey.runs.ledger import RunLedgerStore
    from codey.runs.trace import RunTraceStore
    from codey.runs.work_checkpoint import WorkCheckpointStore
    from codey.runtime.core.api_selection import ApiRunSelection
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.execution_output_store import ExecutionOutputStore
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.changes import ChangeTracker
    from codey.workspace.facts import ProjectFactsStore
    from codey.workspace.revision import WorkspaceRevisionStore


@dataclass(frozen=True)
class TaskSubmissionStores:
    """Resources captured when a task submission is assembled."""

    project_facts: ProjectFactsStore
    work_checkpoints: WorkCheckpointStore
    workspace_revisions: WorkspaceRevisionStore
    run_ledgers: RunLedgerStore | None
    run_traces: RunTraceStore | None
    managed_outputs: ManagedOutputStore | None
    knowledge_store: KnowledgeStore | None
    runtime_mutations: RuntimeMutationLine
    runtime_effects: RuntimeEffectStore
    execution_outputs: ExecutionOutputStore | None = None


class ActiveRun(Protocol):
    @property
    def run_id(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def project(self) -> str | None: ...


class RunCoordination(Protocol):
    def bind_api_selection(self, run_id: str, config: ApiRunSelection) -> None: ...
    def set_last_provider_failure(self, value: ProviderFailure | None) -> None: ...
    def stop_flag_for(self, run_id: str) -> threading.Event: ...


class RunProviders(Protocol):
    @property
    def supervisor(self) -> ProviderSupervisor: ...


class TaskState(Protocol):
    """Run coordination surface (locks, registries, emit)."""

    # Every executable task is durably admitted before entering the task
    # spine; recovery must therefore read the canonical checkpoint directly.
    durable: DurableSession
    memory: MemoryService
    lock: threading.Lock
    @property
    def run_registry(self) -> RunCoordination: ...
    @property
    def providers(self) -> RunProviders: ...
    runtime_mutations: RuntimeMutationLine

    def reserve_run(
        self,
        *,
        session_id: str,
        project: str | None,
        task: str,
        provider_id: str,
        run_id: str = "",
        abort_if_stopped: bool = False,
    ) -> ActiveRun | None: ...
    def release_run(self, run_id: str) -> None: ...
    def current_run(self) -> ActiveRun | None: ...
    def active_run_for(self, *, session_id: str = "", project: str | None = None) -> ActiveRun | None: ...
    def stop_flag_for(self, run_id: str) -> threading.Event: ...
    def finish_run(self, run_id: str, event: dict[str, object]) -> bool: ...
    def start_run(self, run_id: str) -> bool: ...
    def set_run_status(self, status: str) -> None: ...
    def switch_run_provider(self, run_id: str, provider_id: str) -> bool: ...
    def emit(self, event: Mapping[str, object]) -> None: ...
    def get_provider(self, provider_id: str) -> ChatProvider: ...
    def conversation_for(self, session_id: str) -> ConversationContext: ...
    def provider_failover_order(self) -> tuple[str, ...]: ...
    def set_provider_session(self, provider_id: str, session_id: str | None) -> None: ...
    def provider_session_changed(self, provider_id: str, session_id: str) -> bool: ...
    def add_pending_shell_approval(self, approval_id: str, pending: dict[str, object]) -> None: ...
    def resolve_shell_approval(self, approval_id: str) -> dict[str, object] | None: ...
    def expire_stale_shell_approvals(self, active_run_id: str) -> None: ...
    def approval_generation(self) -> int: ...
    def pop_pending_teach(self, teach_id: str) -> dict[str, object] | None: ...
    def is_busy(self) -> bool: ...
    def acquire_project_writer(self, project: str | Path) -> bool: ...
    def release_project_writer(self, project: str | Path) -> None: ...
    def change_tracker_for(self, project: str | Path, *, persistent: bool, task_id: str = "") -> ChangeTracker: ...
    @property
    def self_repair(self) -> SelfRepairSupervisor | None: ...


def stop_flag_for(state: TaskState, run_id: str) -> threading.Event:
    resolver = getattr(state, "stop_flag_for", None)
    if callable(resolver):
        value = resolver(run_id)
        if isinstance(value, threading.Event):
            return value
    registry = getattr(state, "run_registry", None)
    event = getattr(registry, "stop_flag", None)
    return event if isinstance(event, threading.Event) else threading.Event()


__all__ = ["TaskState", "TaskSubmissionStores", "stop_flag_for"]
