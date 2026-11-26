from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from codey.agents.consensus import ConsensusAdvice, ConsensusResult, ConsensusSender
from codey.agents.request import AgentRequest
from codey.knowledge.store import KnowledgeStore
from codey.operations.task_state import TaskState, TaskSubmissionStores
from codey.providers.base import ChatProvider
from codey.providers.diagnostics import ProviderFailure
from codey.reviews.core import ReviewResult
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.core.run_result import RunResult
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.revision import WorkspaceRevisionStore

if TYPE_CHECKING:
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.workspace.changes import ChangeTracker


class CollectChanges(Protocol):
    def __call__(self, project: str | Path, tracker: ChangeTracker | None) -> dict[str, object]: ...


class ReviewTask(Protocol):
    def __call__(self, *, session_id: str, project: str, task: str, writer_summary: str,
                 changes: dict[str, object], recent_log: str, writer_id: str, change_brief: str = "",
                 project_map: str = "", verification_map: str = "", review_impact_map: str | None = None,
                 execution_evidence: str = "", trace_recorder: object | None = None, run_id: str = "",
                 review_source_run_id: str = "", standalone: bool = False) -> tuple[str, ReviewResult] | None: ...


class CaptureProviderFailure(Protocol):
    def __call__(self, *, model: str, action: str, page: object, error: BaseException) -> ProviderFailure: ...


class ConsensusTask(Protocol):
    def __call__(self, *, selected_provider: ConsensusSender, selected_provider_id: str, task: str,
                 context: str = "", draft: str = "", plan: bool = False, draft_first: bool = False,
                 owner_prompt: str = "", trace_recorder: object | None = None) -> ConsensusResult | None: ...


class ProjectAuditTask(Protocol):
    def __call__(self, *, parent_policy: TaskPolicy, project: str | Path, selected_provider: object = None,
                 selected_provider_id: str, task: str, context: str = "",
                 trace_recorder: object | None = None) -> tuple[ConsensusAdvice, ...]: ...


@dataclass(frozen=True)
class TaskRunDeps:
    state: TaskState
    agent_run: Callable[[AgentRequest], RunResult]
    collect_changes: CollectChanges
    run_review: ReviewTask
    capture_provider_failure: CaptureProviderFailure
    workspace_revisions: WorkspaceRevisionStore
    collect_review_changes: CollectChanges | None = None
    run_consensus: ConsensusTask | None = None
    run_project_audit: ProjectAuditTask | None = None
    project_facts: ProjectFactsStore | None = None
    work_checkpoints: WorkCheckpointStore | None = None
    run_ledgers: RunLedgerStore | None = None
    run_traces: RunTraceStore | None = None
    managed_outputs: ManagedOutputStore | None = None
    execution_outputs: ExecutionOutputStore | None = None
    knowledge_store: KnowledgeStore | None = None
    search_factory: Callable[[], object] | None = None
    is_git_repository: Callable[[str | Path], bool] | None = None
    review_fix_turns: int = 12
    review_log_lines: int = 80
    runtime_mutations: RuntimeMutationLine | None = None
    runtime_effects: RuntimeEffectStore | None = None
    connect_provider: Callable[[str], ChatProvider] | None = None
    project_review: bool = True
    bind_provider_handlers: Callable[[], None] | None = None

    @classmethod
    def from_submission_stores(
        cls,
        *,
        state: TaskState,
        stores: TaskSubmissionStores,
        agent_run: Callable[[AgentRequest], RunResult],
        collect_changes: CollectChanges,
        run_review: ReviewTask,
        capture_provider_failure: CaptureProviderFailure,
        collect_review_changes: CollectChanges | None = None,
        run_consensus: ConsensusTask | None = None,
        run_project_audit: ProjectAuditTask | None = None,
        search_factory: Callable[[], object] | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
        connect_provider: Callable[[str], ChatProvider] | None = None,
        project_review: bool = True,
        bind_provider_handlers: Callable[[], None] | None = None,
    ) -> TaskRunDeps:
        """Assemble task dependencies from one explicit resource bundle."""
        return cls(
            state=state,
            agent_run=agent_run,
            collect_changes=collect_changes,
            collect_review_changes=collect_review_changes,
            run_review=run_review,
            capture_provider_failure=capture_provider_failure,
            run_consensus=run_consensus,
            run_project_audit=run_project_audit,
            workspace_revisions=stores.workspace_revisions,
            project_facts=stores.project_facts,
            work_checkpoints=stores.work_checkpoints,
            run_ledgers=stores.run_ledgers,
            run_traces=stores.run_traces,
            managed_outputs=stores.managed_outputs,
            execution_outputs=stores.execution_outputs,
            knowledge_store=stores.knowledge_store,
            search_factory=search_factory,
            is_git_repository=is_git_repository,
            review_fix_turns=review_fix_turns,
            review_log_lines=review_log_lines,
            runtime_mutations=stores.runtime_mutations,
            runtime_effects=stores.runtime_effects,
            connect_provider=connect_provider,
            project_review=project_review,
            bind_provider_handlers=bind_provider_handlers,
        )
