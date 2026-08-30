"""Operation execution context shared by task-mode operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.ghost.work_queue import GhostWorkItem
from codey.providers.diagnostics import ProviderFailure
from codey.runtime.effects import RuntimeOperationState
from codey.runs.ledger import RunLedgerWriter
from codey.runs.work_checkpoint import WorkCheckpoint, WorkCheckpointStore
from codey.runtime.events import RunEvent
from codey.runtime.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission
from codey.workspace.revision import INITIAL_WORKSPACE_REVISION


@dataclass
class RunFrame:
    request: TaskSubmission
    run_id: str
    task_kind: str
    provider: Any | None
    provider_id: str
    project_text: str
    conversation: ConversationContext
    fresh_chat: bool
    handoff: str
    research_handoff: str
    prior_snapshot: ConversationSnapshot
    recovered_owner_prompt: str
    provider_session_changed: bool
    preflight_tried: set[str]
    preflight_switches: int
    trace: Any | None = None


@dataclass
class RunWork:
    recent_events: list[str]
    evidence: ExecutionEvidence
    work_checkpoint: WorkCheckpoint | None = None
    ledger: RunLedgerWriter | None = None
    trace: Any | None = None
    record_agent_events_in_ledger: bool = False
    claimed_work_item: GhostWorkItem | None = None
    analysis_run_payloads: list[dict[str, object]] = field(default_factory=list)
    artifact_payloads: list[dict[str, object]] = field(default_factory=list)
    operation: RuntimeOperationState | None = None
    turns_observed: int = 0
    workspace_revision: int = INITIAL_WORKSPACE_REVISION

    def advance_workspace_revision(self, store: Any, project: str) -> None:
        self.workspace_revision = store.bump(project)
        self.evidence.set_workspace_revision(self.workspace_revision)


@dataclass(frozen=True)
class RunHooks:
    on_event: Callable[[RunEvent], None]
    on_shell_request: Callable[[str, str], None]
    update_checkpoint: Callable[
        [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
        None,
    ]
    record_provider_failure: Callable[[str, ProviderFailure], None]
    append_ledger: Callable[[Callable[[RunLedgerWriter], None]], None]
    provider_failover_order: Callable[[], tuple[str, ...]]
    supervisor: Any | None
    trace: Any | None = None
