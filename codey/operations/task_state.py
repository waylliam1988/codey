"""Structural contract the task spine needs from application state.

``TaskRunDeps.state`` used to be ``Any``, which hid the real coupling between
``operations/`` and ``AppContext``: every ``state.handle_*`` addition
silently widened what the spine could touch. This ``Protocol`` lists exactly
the members the spine (plus ``task_submit`` and the app service modules) may
use -- run coordination, locks, registries, and emit. ``AppContext``
satisfies it structurally.

No runtime import happens here (annotations only, real types under
``TYPE_CHECKING``), so cold start is unaffected.

Rule for growing this surface: add a member only when a ``TaskState``-typed
caller needs it. HTTP-layer needs (ui_state, knowledge, ledgers) stay on the
untyped ``ctx`` in ``api.py`` on purpose -- a second protocol there would just
reduplicate ``AppContext``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from codey.agents.handoff import ConversationContext
    from codey.app.approval_registry import ApprovalRegistry
    from codey.app.provider_registry import ProviderRegistry
    from codey.app.run_registry import RunRegistry, RunSnapshot
    from codey.knowledge.store import KnowledgeStore
    from codey.repairs.self_repair import SelfRepairSupervisor
    from codey.research.evidence_ledger import EvidenceLedgerStore
    from codey.runs.ledger import RunLedgerStore
    from codey.runs.trace import RunTraceStore
    from codey.runs.work_checkpoint import WorkCheckpointStore
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
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
    evidence_ledgers: EvidenceLedgerStore | None
    managed_outputs: ManagedOutputStore | None
    knowledge_store: KnowledgeStore | None
    runtime_mutations: RuntimeMutationLine
    runtime_effects: RuntimeEffectStore


class TaskState(Protocol):
    """Run coordination surface (locks, registries, emit)."""

    lock: threading.Lock
    _shell_spawn_gate: threading.Lock
    run_registry: RunRegistry
    providers: ProviderRegistry
    approvals: ApprovalRegistry
    runtime_log: RuntimeSessionLog
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
    ) -> RunSnapshot | None: ...
    def release_run(self, run_id: str) -> None: ...
    def current_run(self) -> RunSnapshot | None: ...
    def finish_run(self, run_id: str, event: dict[str, Any]) -> bool: ...
    def start_run(self, run_id: str) -> bool: ...
    def set_run_status(self, status: str) -> None: ...
    def switch_run_provider(self, run_id: str, provider_id: str) -> bool: ...
    def emit(self, event: dict[str, Any]) -> None: ...
    def get_provider(self, provider_id: str) -> Any: ...
    def conversation_for(self, session_id: str) -> ConversationContext: ...
    def provider_failover_order(self) -> tuple[str, ...]: ...
    def set_provider_session(self, provider_id: str, session_id: str | None) -> None: ...
    def provider_session_changed(self, provider_id: str, session_id: str) -> bool: ...
    def visible_session_excerpt(self, session_id: str, current_request: str = "") -> str: ...
    def add_pending_shell_approval(self, approval_id: str, pending: dict[str, Any]) -> None: ...
    def expire_stale_shell_approvals(self, active_run_id: str) -> None: ...
    def approval_generation(self) -> int: ...
    def pop_pending_teach(self, teach_id: str) -> dict[str, Any] | None: ...
    def is_busy(self) -> bool: ...
    def wait_for_ghost_sleep(self, timeout: float | None = None) -> bool: ...
    def acquire_project_writer(self, project: str | Path) -> bool: ...
    def release_project_writer(self, project: str | Path) -> None: ...
    def change_tracker_for(self, project: str | Path, *, persistent: bool) -> ChangeTracker: ...
    @property
    def self_repair(self) -> SelfRepairSupervisor | None: ...


class TaskSubmissionState(TaskState, Protocol):
    """State plus resources needed to assemble a task run."""

    @property
    def task_submission_stores(self) -> TaskSubmissionStores: ...


__all__ = ["TaskState", "TaskSubmissionState", "TaskSubmissionStores"]
