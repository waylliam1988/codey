"""Shared context for project completion phases.

Owns dataclasses, limits, and shared helpers (checkpoint view, runtime
commit, tool-event projection, analysis-run projection). No imports from
writer/review/enforcement/flow to keep the split acyclic. Production code
imports helpers from here, never via ``project_completion_flow`` re-exports.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from codey.agents.tools import AgentToolFns
from codey.agents.writer_failover import WriterFailoverRunner
from codey.completion.contract import completion_proof_trace_payload
from codey.completion.edit_integrity import EditIntegrityObservation
from codey.completion.engine import CompletionEngine, blocked_note
from codey.completion.repair_context import RepairContextProjection
from codey.completion.verification_map import render_verification_map
from codey.completion.verification_policy import VerificationCandidate
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.prompting import (
    record_secondary_input_prepared_trace as _record_secondary_input_prepared_trace,
)
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.operations.task_state import TaskState
from codey.providers.diagnostics import ProviderFailure
from codey.runs.work_checkpoint import CheckpointCheck, WorkCheckpointStore
from codey.runtime.core.run_result import RunResult
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.write.mutation_line import RuntimeOperationState
from codey.storage.managed_outputs import (
    ManagedOutputStore,
    run_command_with_managed_output,
)
from codey.workspace.change_brief import ChangeBrief
from codey.workspace.config import ProjectConfigLoadResult, ProjectVerificationCommand
from codey.workspace.facts import ProjectFactsStore, VerifiedCommand


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
    ctx.work.operation = cast(RuntimeOperationState, committed)


@dataclass
class ProjectRun:
    deps: ProjectCompletionDeps
    frame: RunFrame
    work: RunWork
    hooks: RunHooks
    project: str
    state: TaskState
    config_result: ProjectConfigLoadResult | None = None
    research_result: Any = None
    research_pipeline_result: Any = None
    request: Any = None
    context_builder: ProjectTaskContextBuilder | None = None
    project_context: Any = None
    verified_facts: str = ""
    verification_verified_commands: tuple[VerifiedCommand, ...] = ()
    verification_candidates: tuple[VerificationCandidate, ...] = ()
    resumed_verification_commands: tuple[CheckpointCheck, ...] = ()
    configured_verification_commands: tuple[ProjectVerificationCommand, ...] = ()
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
    task_session: Any = None
    research_tools: Any = None
    blocked_reason: str = ""
    repaired_once: bool = False
    writer_attempt_index: int = 0
    receipt: Any = None

    @property
    def task_state(self) -> TaskState:
        """Return the state installed by project context preparation."""
        if self.state is None:
            raise ProjectRuntimeMutationError("project task state is not prepared")
        return self.state


def handle_project_tool_event(
    deps: ProjectCompletionDeps,
    *,
    event: Any,
    project: str,
    work: RunWork,
    run_id: str,
    update_checkpoint: Callable[
        [Callable[[Any, Any], Any]],
        None,
    ],
) -> None:
    """Shared tool-event projection (single owner; hooks must not duplicate)."""
    call = getattr(event, "call", None)
    outcome = getattr(event, "outcome", None)
    if call is None or outcome is None:
        return
    name = str(getattr(call, "name", "") or "")
    if name == "run":
        args = getattr(call, "args", {}) if isinstance(getattr(call, "args", {}), dict) else {}
        command = str(args.get("command") or "")
        cwd = str(args.get("path") or ".")
        from codey.utils.refs import strict_run_success

        ok = strict_run_success(
            getattr(outcome, "ok", None), getattr(outcome, "exit_code", None)
        )
        try:
            meta = getattr(event, "metadata", {}) or {}
            tool_index = int(meta.get("tool_index") or 0) if isinstance(meta, dict) else 0
        except (TypeError, ValueError):
            tool_index = 0
        tool_id = f"{getattr(event, 'turn', 0)}:{max(0, tool_index)}"
        if ok and deps.persistence.project_facts is not None:
            with contextlib.suppress(OSError, ValueError):
                deps.persistence.project_facts.record_success(project, cwd, command)
        update_checkpoint(
            lambda store, item: store.record_run(
                item,
                command=command,
                cwd=cwd,
                ok=ok,
                workspace_revision=work.workspace_revision,
                workspace_fingerprint=work.workspace_fingerprint,
            )
        )
        record_analysis_run(
            work=work,
            project=project,
            run_id=run_id,
            tool_id=tool_id,
            tool_name=name,
            command=command,
            cwd=cwd,
            ok=ok,
            outcome=outcome,
        )
    elif name == "edit":
        outcome_ok = getattr(outcome, "ok", None)
        outcome_changed = getattr(outcome, "changed", None)
        if type(outcome_ok) is not bool or outcome_ok is not True:
            return
        if type(outcome_changed) is not bool or outcome_changed is not True:
            return
        args = getattr(call, "args", {}) if isinstance(getattr(call, "args", {}), dict) else {}
        rel = str(args.get("path") or "")
        update_checkpoint(lambda store, item: store.record_edit(item, rel))


def record_analysis_run(
    *,
    work: RunWork,
    project: str,
    run_id: str,
    tool_id: str,
    tool_name: str,
    command: str,
    cwd: str,
    ok: bool,
    outcome: Any,
) -> None:
    """Project one audited run-command execution into the run trace.

    Fail-open by contract: projection or trace failures never affect the
    running task, its receipt, or the model-visible tool result.
    """
    from codey.research.analysis_run import analysis_run_record
    from codey.research.artifact_lineage import artifact_ref_from_managed_output
    from codey.research.reproducibility import build_reproducibility_capsule
    from codey.runs.trace_schema import MAX_ANALYSIS_RUNS, MAX_ARTIFACT_REFS
    from codey.runtime.core import cancellation

    trace = work.trace
    if trace is None or not command:
        return
    try:
        audit = outcome.audit if isinstance(getattr(outcome, "audit", None), Mapping) else {}
        if not audit.get("command_started_at"):
            return
        managed = outcome.managed_output() if callable(getattr(outcome, "managed_output", None)) else {}
        record = analysis_run_record(
            {
                "run_id": run_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "command": command,
                "cwd": cwd,
                "project": project,
                "exit_code": getattr(outcome, "exit_code", None),
                "ok": ok,
                "started_at": audit.get("command_started_at"),
                "finished_at": audit.get("command_finished_at"),
                "duration_ms": audit.get("command_duration_ms"),
                "managed_output": dict(managed) if managed else {},
                "capture_truncated": bool(audit.get("capture_truncated")),
            }
        )
        if record is None:
            return
        record_payload = record.to_payload()
        trace.record_analysis_run(record_payload)
        work.analysis_run_payloads.append(record_payload)
        if len(work.analysis_run_payloads) > MAX_ANALYSIS_RUNS:
            del work.analysis_run_payloads[:-MAX_ANALYSIS_RUNS]

        artifact_payload: dict[str, object] | None = None
        if managed:
            artifact = artifact_ref_from_managed_output(
                {
                    **managed,
                    "origin_run_id": run_id,
                    "produced_by": record.analysis_run_id,
                }
            )
            if artifact is not None:
                artifact_payload = artifact.to_payload()
                trace.record_artifact_refs([artifact_payload])
                work.artifact_payloads.append(artifact_payload)
                if len(work.artifact_payloads) > MAX_ARTIFACT_REFS:
                    del work.artifact_payloads[:-MAX_ARTIFACT_REFS]

        capsule = build_reproducibility_capsule(
            run_id=run_id,
            analysis_runs=work.analysis_run_payloads,
            artifacts=work.artifact_payloads,
        )
        if capsule is not None:
            trace.record_reproducibility_capsule(capsule.to_payload())
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return


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
    "blocked_result",
    "commit_runtime_operation",
    "handle_project_tool_event",
    "managed_tool_fns",
    "project_has_user_files",
    "record_analysis_run",
    "record_completion_proof_trace",
    "record_edit_integrity_trace",
    "record_review_input_prepared_trace",
    "refresh_checkpoint_view",
    "safe_verification_map",
]
