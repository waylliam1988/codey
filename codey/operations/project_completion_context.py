"""Shared context for project completion phases.

Owns dataclasses, limits, and pure helpers. No imports from
writer/review/enforcement/flow to keep the split acyclic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from codey.agents.tools import AgentToolFns
from codey.agents.writer_failover import WriterFailoverRunner
from codey.completion.contract import completion_proof_trace_payload
from codey.completion.edit_integrity import EditIntegrityObservation
from codey.completion.engine import CompletionEngine, blocked_note
from codey.completion.repair_context import RepairContextProjection
from codey.completion.verification_map import render_verification_map
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.prompting import (
    record_secondary_input_prepared_trace as _record_secondary_input_prepared_trace,
)
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.operations.task_state import TaskState
from codey.providers.diagnostics import ProviderFailure
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.storage.managed_outputs import (
    ManagedOutputStore,
    run_command_with_managed_output,
)
from codey.workspace.change_brief import ChangeBrief
from codey.workspace.config import ProjectConfigLoadResult
from codey.workspace.facts import ProjectFactsStore


def _default_is_git_repository(_project: str | Path) -> bool:
    return False


@dataclass(frozen=True)
class AgentAccess:
    run: Callable
    capture_provider_failure: Callable[..., ProviderFailure]
    run_consensus: Callable | None = None
    run_project_audit: Callable | None = None
    is_git_repository: Callable[[str | Path], bool] = _default_is_git_repository


@dataclass(frozen=True)
class PersistenceAccess:
    project_facts: ProjectFactsStore | None = None
    work_checkpoints: WorkCheckpointStore | None = None
    managed_outputs: ManagedOutputStore | None = None
    knowledge_store: KnowledgeStore | None = None
    search_factory: Callable[[], object] | None = None


@dataclass(frozen=True)
class VerificationAccess:
    collect_changes: Callable
    workspace_revisions: Any


@dataclass(frozen=True)
class ReviewAccess:
    run: Callable
    review_fix_turns: int = 12
    review_log_lines: int = 80


@dataclass(frozen=True)
class RuntimeAccess:
    mutations: Any = None
    effects: Any = None
    tool_result_delivery: Any = None


@dataclass(frozen=True)
class ProjectCompletionDeps:
    state: TaskState
    agent: AgentAccess
    verification: VerificationAccess
    review: ReviewAccess
    runtime: RuntimeAccess
    persistence: PersistenceAccess = field(default_factory=PersistenceAccess)


class ProjectRuntimeMutationError(RuntimeError):
    """A required project runtime fact could not be committed."""


NEW_PROJECT_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".codey",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}
NEW_PROJECT_IGNORED_FILES = {
    ".DS_Store",
    "Thumbs.db",
    ".gitignore",
    ".gitattributes",
    ".gitkeep",
}


# One bounded repair round is the whole of 0.4.13: enough to prove the
# repair-context loop works, small enough that a model stuck in a wrong local
# optimum cannot turn Codey into a self-consuming machine.
MAX_COMPLETION_REPAIR_ROUNDS = 1

COMPLETION_REPAIR_FOLLOWUP = (
    "Continue with the established project and JSON tool protocol.\n\n"
    "Your previous completion claim did not pass local verification. The "
    "completion repair context section of this message lists the observed "
    "failure facts. Decide and perform the next local step yourself."
)


def record_completion_proof_trace(
    trace: Any | None,
    proof: object,
) -> None:
    if proof is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_completion_proof", completion_proof_trace_payload(proof))
    sink.call("flush")


def record_edit_integrity_trace(
    trace: Any | None,
    observation: EditIntegrityObservation | None,
) -> None:
    if observation is None:
        return
    sink = FailOpenPromptTrace(trace)
    sink.call("record_edit_integrity", observation.to_payload())
    sink.call("flush")


def blocked_result(result: RunResult, reason: str) -> RunResult:
    """Turn a claimed-done result into an honest blocked stop."""
    note = blocked_note(reason)
    summary = result.summary.strip()
    return replace(
        result,
        stop_reason="blocked",
        summary=f"{summary}\n\n[{note}]" if summary else f"[{note}]",
    )


def safe_verification_map(
    project: str,
    changes: dict,
    checks: tuple[object, ...],
    project_map: str,
    recommended_commands: tuple[str, ...] = (),
) -> str:
    try:
        return render_verification_map(
            project,
            changes,
            checks_after_last_change=checks,
            project_map=project_map,
            recommended_commands=recommended_commands,
        )
    except Exception:
        return ""


def record_review_input_prepared_trace(
    trace: Any | None,
    *,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    change_brief: str,
    project_map: str,
    verification_map: str,
    review_impact_map: str,
    execution_evidence: str,
) -> None:
    if trace is None:
        return
    _record_secondary_input_prepared_trace(
        trace,
        "review",
        task=task,
        writer_summary=writer_summary,
        diff=(changes or {}).get("diff", "") if isinstance(changes, dict) else "",
        recent_log=recent_log,
        change_brief=change_brief,
        project_map=project_map,
        verification_map=verification_map,
        review_impact_map=review_impact_map,
        execution_evidence=execution_evidence,
    )


def managed_tool_fns(
    deps: ProjectCompletionDeps,
    *,
    session_id: str,
    run_id: str,
) -> AgentToolFns | None:
    if deps.persistence.managed_outputs is None:
        return None

    def run_command(
        root: Path,
        rel: str,
        command: str,
        tool_id: str,
        _permission_profile: str,
        _phase: str,
    ):
        return run_command_with_managed_output(
            root,
            rel,
            command,
            permission_profile=_permission_profile,
            phase=_phase,
            store=deps.persistence.managed_outputs,
            session_id=session_id,
            run_id=run_id,
            tool_id=tool_id,
        )

    return AgentToolFns(run_command_with_context=run_command)


def project_has_user_files(project: str | Path) -> bool:
    """Return true when a project has real user files worth inspecting first."""

    stack = [Path(project).expanduser()]
    while stack:
        current = stack.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name in NEW_PROJECT_IGNORED_DIRS:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    stack.append(entry)
                elif entry.is_file() and name not in NEW_PROJECT_IGNORED_FILES:
                    return True
            except OSError:
                continue
    return False


def _bullet_lines(values: tuple[str, ...]) -> str:
    if not values:
        return "- (none)"
    return "\n".join(f"- {item}" for item in values)


def refresh_checkpoint_view(ctx: ProjectRun) -> Any:
    """Shared checkpoint refresh (single owner; phases must not duplicate)."""
    assert ctx.context_builder is not None
    refreshed = ctx.context_builder.refresh_checkpoint(ctx.work.work_checkpoint)
    if refreshed.item is None:
        ctx.checkpoint_prompt = ""
        ctx.resumed_changed_files = ()
        ctx.resumed_successful_checks = ()
    else:
        ctx.work.work_checkpoint = refreshed.item
        ctx.checkpoint_prompt = refreshed.prompt
        ctx.resumed_changed_files = refreshed.changed_files
        ctx.resumed_successful_checks = refreshed.successful_checks
    if refreshed.workspace_changed:
        ctx.work.advance_workspace_revision(
            ctx.deps.verification.workspace_revisions,
            ctx.project,
            ignored_paths=ctx.configured_ignored_paths,
        )
    from codey.agents.writer_failover import CheckpointView

    return CheckpointView(
        prompt=ctx.checkpoint_prompt,
        changed_files=ctx.resumed_changed_files,
        successful_checks=ctx.resumed_successful_checks,
    )


def commit_runtime_operation(ctx: ProjectRun, step: str, commit: Callable[[Any, str, str], object | None]) -> None:
    """Shared runtime commit (single owner; phases must not duplicate)."""
    from codey.runtime.core.operation_state import RuntimeOperationTransitionError
    from codey.runtime.log.entries import RuntimeLogError

    operation = ctx.work.operation
    mutations = ctx.deps.runtime.mutations
    if operation is None and mutations is None:
        return
    if operation is None:
        raise ProjectRuntimeMutationError(f"runtime operation missing while committing {step}")
    if mutations is None:
        raise ProjectRuntimeMutationError(f"runtime mutation line missing while committing {step}")
    try:
        committed = commit(mutations, operation.session_id, operation.run_id)
    except (OSError, ValueError, RuntimeOperationTransitionError, RuntimeLogError) as exc:
        raise ProjectRuntimeMutationError(f"runtime mutation failed while committing {step}: {exc}") from exc
    if committed is None:
        raise ProjectRuntimeMutationError(f"runtime mutation produced no operation while committing {step}")
    ctx.work.operation = committed


@dataclass
class ProjectRun:
    deps: ProjectCompletionDeps
    frame: RunFrame
    work: RunWork
    hooks: RunHooks
    project: str
    config_result: ProjectConfigLoadResult | None = None
    research_result: Any = None
    research_pipeline_result: Any = None
    state: TaskState | None = None
    request: Any = None
    context_builder: ProjectTaskContextBuilder | None = None
    project_context: Any = None
    verified_facts: str = ""
    verification_verified_commands: tuple[str, ...] = ()
    verification_candidates: tuple[Any, ...] = ()
    resumed_verification_commands: tuple[str, ...] = ()
    configured_verification_commands: tuple[str, ...] = ()
    configured_ignored_paths: tuple[str, ...] = ()
    project_map_chars: int = 0
    project_map: str = ""
    checkpoint_prompt: str = ""
    resumed_changed_files: tuple[str, ...] = ()
    resumed_successful_checks: tuple[Any, ...] = ()
    agent_task: str = ""
    change_brief: ChangeBrief | None = None
    agent_fresh_chat: bool = False
    has_user_files: bool = False
    used_project_audit: bool = False
    tracker: Any = None
    tried_writers: set[str] | None = None
    repair_projection: RepairContextProjection | None = None
    failover: WriterFailoverRunner | None = None
    result: RunResult | None = None
    inherited_green: bool = False
    task_changed: bool = False
    task_changes: dict | None = None
    task_changes_dirty: bool = False
    review_cycle: Any = None
    files: tuple[str, ...] = ()
    selected_check: Any = None
    checkpoint_green: bool = False
    verification_forbidden: bool = False
    completion_engine: CompletionEngine | None = None
    decision: Any = None
    integrity: EditIntegrityObservation | None = None
    proof: Any = None
    blocked_reason: str = ""
    repaired_once: bool = False
    writer_attempt_index: int = 0
    receipt: Any = None


# Backward-compat alias for the pre-split private name.
_ProjectRun = ProjectRun


__all__ = [
    "AgentAccess",
    "COMPLETION_REPAIR_FOLLOWUP",
    "MAX_COMPLETION_REPAIR_ROUNDS",
    "NEW_PROJECT_IGNORED_DIRS",
    "NEW_PROJECT_IGNORED_FILES",
    "PersistenceAccess",
    "ProjectCompletionDeps",
    "ProjectRun",
    "ProjectRuntimeMutationError",
    "ReviewAccess",
    "RuntimeAccess",
    "VerificationAccess",
    "_ProjectRun",
    "blocked_result",
    "commit_runtime_operation",
    "managed_tool_fns",
    "project_has_user_files",
    "record_completion_proof_trace",
    "record_edit_integrity_trace",
    "record_review_input_prepared_trace",
    "refresh_checkpoint_view",
    "safe_verification_map",
]
