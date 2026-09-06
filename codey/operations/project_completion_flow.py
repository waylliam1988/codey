from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any

from codey.agents.consensus import render_project_context
from codey.agents.protocol import task_forbids_verification
from codey.agents.request import AgentRequest
from codey.agents.runner import RunResult
from codey.agents.tools import AgentToolFns
from codey.agents.writer_failover import (
    CheckpointView,
    WriterAttempt,
    WriterFailoverRunner,
)
from codey.completion.contract import completion_proof_trace_payload
from codey.completion.edit_integrity import EditIntegrityObservation
from codey.completion.edit_scope import changed_paths_from_changes
from codey.completion.engine import CompletionEngine, blocked_note
from codey.completion.repair_context import (
    RepairContextProjection,
    project_repair_context,
    repair_candidate,
)
from codey.completion.verification import (
    STANCE_FRESH_PASS,
    STANCE_INHERITED_PASS,
    decisive_failure_fact,
)
from codey.completion.verification_map import render_verification_map
from codey.completion.verification_policy import (
    select_verification_candidate,
    selected_verification_candidate_lines,
    verification_candidate_lines,
)
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.knowledge.note import KnowledgeNote
from codey.knowledge.store import KnowledgeStore
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.prompting import (
    record_secondary_input_prepared_trace as _record_secondary_input_prepared_trace,
)
from codey.operations.provider_preflight import (
    provider_fallback_policy_decision as _provider_fallback_policy_decision,
)
from codey.operations.research_flow import research_payload as _research_payload
from codey.operations.result import ModeOutcome
from codey.providers import PROVIDER_LABELS
from codey.providers.capabilities import rank_providers
from codey.providers.diagnostics import ProviderActionError, ProviderFailure
from codey.providers.supervisor import run_half_open_canary
from codey.research.analysis_run import analysis_run_record
from codey.research.artifact_lineage import artifact_ref_from_managed_output
from codey.research.reproducibility import build_reproducibility_capsule
from codey.reviews.coordinator import ReviewCoordinator, change_state
from codey.reviews.impact_map import safe_review_impact_map
from codey.runs.receipt import VERIFICATION_TRUST_TRUSTED, build_task_receipt
from codey.runs.trace import MAX_ANALYSIS_RUNS, MAX_ARTIFACT_REFS
from codey.runs.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
)
from codey.runtime import cancellation
from codey.runtime.operation_state import (
    LEAF_COMPLETION_PROOF_RECORDED,
    RuntimeOperationTransitionError,
)
from codey.runtime.events import RunEvent
from codey.runtime.prompt_envelope import FailOpenPromptTrace
from codey.runtime.terminalizer import task_done_event
from codey.storage.managed_outputs import (
    ManagedOutputStore,
    run_command_with_managed_output,
)
from codey.task.kind import writer_failover_mode as _writer_failover_mode
from codey.workspace.change_brief import (
    ChangeBrief,
    new_project_change_brief,
    project_audit_change_brief,
)
from codey.workspace.config import ProjectConfigLoadResult, preferred_provider_for
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.task_context import (
    ProjectTaskContextBuilder,
    safe_project_map,
    safe_verification_candidates,
)


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
    state: Any
    agent: AgentAccess
    verification: VerificationAccess
    review: ReviewAccess
    runtime: RuntimeAccess
    persistence: PersistenceAccess = field(default_factory=PersistenceAccess)


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


__all__ = [
    "AgentAccess",
    "COMPLETION_REPAIR_FOLLOWUP",
    "MAX_COMPLETION_REPAIR_ROUNDS",
    "PersistenceAccess",
    "ProjectCompletionDeps",
    "ReviewAccess",
    "RuntimeAccess",
    "VerificationAccess",
    "blocked_result",
    "handle_project_tool_event",
    "managed_tool_fns",
    "project_has_user_files",
    "record_completion_proof_trace",
    "record_edit_integrity_trace",
    "record_project_memory",
    "record_review_input_prepared_trace",
    "run_project_mode",
    "safe_verification_map",
]


@dataclass
class _ProjectRun:
    deps: ProjectCompletionDeps
    frame: RunFrame
    work: RunWork
    hooks: RunHooks
    project: str
    config_result: ProjectConfigLoadResult | None = None
    research_result: Any = None
    research_pipeline_result: Any = None
    state: Any = None
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
    receipt: Any = None


def _prepare_project_context(ctx: _ProjectRun) -> None:
    ctx.state = ctx.deps.state
    ctx.request = ctx.frame.request
    ctx.agent_task = ctx.request.task
    ctx.agent_fresh_chat = ctx.frame.fresh_chat
    if ctx.work.ledger is not None:
        ctx.work.record_agent_events_in_ledger = True
    ctx.context_builder = ProjectTaskContextBuilder(
        project_facts=ctx.deps.persistence.project_facts,
        work_checkpoints=ctx.deps.persistence.work_checkpoints,
        knowledge_store=ctx.deps.persistence.knowledge_store,
        config_result=ctx.config_result,
    )
    ctx.project_context = ctx.context_builder.build(
        project=ctx.project,
        task=ctx.request.task,
        session_id=ctx.request.session_id,
        run_id=ctx.frame.run_id,
        continue_task=ctx.request.continue_task,
        provider_session_changed=ctx.frame.provider_session_changed,
    )
    ctx.verified_facts = ctx.project_context.verified_facts
    ctx.verification_verified_commands = (
        ctx.project_context.verification_verified_commands
    )
    ctx.verification_candidates = ctx.project_context.verification_candidates
    ctx.resumed_verification_commands = (
        ctx.project_context.resumed_verification_commands
    )
    ctx.configured_verification_commands = (
        ctx.project_context.configured_verification_commands
    )
    ctx.configured_ignored_paths = ctx.project_context.configured_ignored_paths
    ctx.project_map_chars = ctx.project_context.project_map_chars
    ctx.project_map = ctx.project_context.project_map
    ctx.work.work_checkpoint = ctx.project_context.checkpoint.item
    ctx.checkpoint_prompt = ctx.project_context.checkpoint.prompt
    ctx.resumed_changed_files = ctx.project_context.checkpoint.changed_files
    ctx.resumed_successful_checks = ctx.project_context.checkpoint.successful_checks
    ctx.work.evidence.seed_checks(ctx.project_context.checkpoint.seed_checks)
    if ctx.project_context.checkpoint.workspace_changed:
        ctx.work.advance_workspace_revision(
            ctx.deps.verification.workspace_revisions,
            ctx.project,
            ignored_paths=ctx.configured_ignored_paths,
        )
    ctx.agent_task = ctx.request.task
    ctx.agent_fresh_chat = ctx.frame.fresh_chat
    _prepare_new_project_context(ctx)
    key = str(Path(ctx.project).expanduser().resolve())
    ctx.tracker = ctx.state.change_tracker_for(
        key,
        persistent=not ctx.deps.agent.is_git_repository(key),
    )
    ctx.tried_writers = set(ctx.frame.preflight_tried)


def _prepare_new_project_context(ctx: _ProjectRun) -> None:
    ctx.has_user_files = project_has_user_files(ctx.project)
    if ctx.deps.agent.run_consensus is not None and not ctx.has_user_files:
        context = render_project_context(
            ctx.frame.conversation.snapshot,
            ctx.verified_facts,
            project_map=ctx.project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                ctx.frame.trace,
                "consensus",
                task=ctx.request.task,
                context=context,
            )
            planned = ctx.deps.agent.run_consensus(
                selected_provider=ctx.frame.provider,
                selected_provider_id=ctx.frame.provider_id,
                task=ctx.request.task,
                context=context,
                plan=True,
                draft_first=True,
                trace_recorder=ctx.frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            ctx.state.set_provider_session(ctx.frame.provider_id, None)
            ctx.agent_fresh_chat = True
            planned = None
        if planned is not None:
            ctx.change_brief = new_project_change_brief(ctx.request.task, planned.answer)
            ctx.agent_task = ctx.change_brief.apply_to_task(ctx.request.task)
            ctx.agent_fresh_chat = True
    elif ctx.deps.agent.run_project_audit is not None and ctx.has_user_files:
        context = render_project_context(
            ctx.frame.conversation.snapshot,
            ctx.verified_facts,
            project_map=ctx.project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                ctx.frame.trace,
                "project_audit",
                task=ctx.request.task,
                context=context,
            )
            reports = ctx.deps.agent.run_project_audit(
                project=ctx.project,
                selected_provider=ctx.frame.provider,
                selected_provider_id=ctx.frame.provider_id,
                task=ctx.request.task,
                context=context,
                trace_recorder=ctx.frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reports = ()
        if reports:
            ctx.change_brief = project_audit_change_brief(ctx.request.task, reports)
            ctx.agent_task = ctx.change_brief.apply_to_task(ctx.request.task)
            ctx.used_project_audit = True


def _refresh_checkpoint_view(ctx: _ProjectRun) -> CheckpointView:
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
    return CheckpointView(
        prompt=ctx.checkpoint_prompt,
        changed_files=ctx.resumed_changed_files,
        successful_checks=ctx.resumed_successful_checks,
    )


def _writer_verification_candidates(ctx: _ProjectRun) -> tuple[Any, ...]:
    return safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )


def _on_writer_event(
    ctx: _ProjectRun,
    note_turn: Callable[[int], None],
    event: RunEvent,
) -> None:
    note_turn(event.turn)
    ctx.hooks.on_event(event)


def _run_one_writer_attempt(
    ctx: _ProjectRun,
    spec: WriterAttempt,
    note_turn: Callable[[int], None],
) -> RunResult:
    recovered_outcomes = ctx.frame.recovered_tool_outcomes
    ctx.frame.recovered_tool_outcomes = ()
    recovered_batch_id = ctx.frame.recovered_tool_result_batch_id
    ctx.frame.recovered_tool_result_batch_id = ""
    return ctx.deps.agent.run(AgentRequest(
        provider=spec.provider,
        project=Path(ctx.project),
        task=spec.task,
        max_turns=spec.remaining_turns,
        on_event=partial(_on_writer_event, ctx, note_turn),
        on_shell_request=ctx.hooks.on_shell_request,
        stop_flag=ctx.state.run_registry.stop_flag,
        fresh_chat=spec.fresh_chat,
        strict_fresh_chat=spec.strict_fresh_chat,
        change_tracker=ctx.tracker,
        conversation=ctx.frame.conversation,
        provider_id=spec.provider_id,
        handoff=spec.handoff,
        project_facts=ctx.verified_facts,
        research_context=ctx.project_context.research_context,
        project_map=ctx.project_map,
        project_config_warnings=ctx.project_context.project_config_warnings,
        work_checkpoint=spec.checkpoint.prompt,
        verification_candidates=ctx.verification_candidates,
        verification_candidate_loader=partial(_writer_verification_candidates, ctx),
        verification_changed_files=spec.checkpoint.changed_files,
        verification_successful_checks=spec.checkpoint.successful_checks,
        ghost_directive="",
        ghost_continuity="",
        completion_repair_context=(
            ctx.repair_projection.prompt_text
            if ctx.repair_projection is not None
            else ""
        ),
        completion_repair_context_payload=(
            ctx.repair_projection.to_payload()
            if ctx.repair_projection is not None
            else None
        ),
        permission_profile="coding_writer",
        tool_fns=managed_tool_fns(
            ctx.deps,
            session_id=ctx.request.session_id,
            run_id=ctx.frame.run_id,
        ),
        trace_recorder=ctx.frame.trace,
        session_id=ctx.request.session_id,
        run_id=ctx.frame.run_id,
        runtime_effects=ctx.deps.runtime.effects,
        tool_result_delivery=ctx.deps.runtime.tool_result_delivery,
        runtime_mutations=ctx.deps.runtime.mutations,
        recovered_tool_outcomes=recovered_outcomes,
        recovered_tool_result_batch_id=recovered_batch_id,
    ))



def _select_next_writer(ctx: _ProjectRun, excluded: set[str]) -> str | None:
    mode = _writer_failover_mode(ctx.frame.task_kind)
    preference = (
        preferred_provider_for(ctx.config_result.config, mode)
        if ctx.config_result is not None
        else ""
    )
    ranked_order = rank_providers(
        ctx.hooks.provider_failover_order(),
        mode=mode,
        preferred=preference,
    )
    if ctx.hooks.supervisor is not None:
        return ctx.hooks.supervisor.select("", ranked_order, excluded=excluded)
    return next((item for item in ranked_order if item not in excluded), None)


def _capture_writer_failure(
    ctx: _ProjectRun,
    pid: str,
    action: str,
    error: BaseException,
) -> ProviderFailure:
    return ctx.deps.agent.capture_provider_failure(
        model=PROVIDER_LABELS.get(pid, pid),
        action=action,
        page=None,
        error=error,
    )


def _on_writer_switch(ctx: _ProjectRun, next_provider_id: str) -> None:
    previous_provider_id = ctx.frame.provider_id
    ctx.state.switch_run_provider(ctx.frame.run_id, next_provider_id)
    ctx.hooks.append_ledger(
        lambda ledger: ledger.append(
            "provider_switched",
            from_provider=previous_provider_id,
            to_provider=next_provider_id,
            phase="writer_failover",
            reason="provider_failure",
        )
    )
    FailOpenPromptTrace(ctx.hooks.trace).call(
        "record_fallback",
        from_provider=previous_provider_id,
        to_provider=next_provider_id,
        phase="writer_failover",
        reason_code="provider_failure",
    )
    FailOpenPromptTrace(ctx.hooks.trace).call(
        "record_policy_decision",
        _provider_fallback_policy_decision(
            from_provider=previous_provider_id,
            to_provider=next_provider_id,
            phase="writer_failover",
        ),
    )
    ctx.frame.conversation.update_snapshot(
        replace(
            ctx.frame.conversation.snapshot,
            provider_id=next_provider_id,
            blocker="",
        )
    )


def _close_provider(item: Any) -> None:
    item.close()


def _needs_no_canary(_pid: str) -> bool:
    return False


def _record_no_success(_pid: str) -> None:
    return None


def _clear_provider_session(ctx: _ProjectRun, pid: str) -> None:
    ctx.state.set_provider_session(pid, None)


def _run_writer_canary(ctx: _ProjectRun, pid: str, item: Any) -> bool:
    return run_half_open_canary(pid, item, ctx.hooks.supervisor)


def _build_writer_failover(ctx: _ProjectRun) -> WriterFailoverRunner:
    assert ctx.tried_writers is not None
    return WriterFailoverRunner(
        provider=ctx.frame.provider,
        provider_id=ctx.frame.provider_id,
        switches=ctx.frame.preflight_switches,
        tried=ctx.tried_writers,
        attempt=partial(_run_one_writer_attempt, ctx),
        select_next=partial(_select_next_writer, ctx),
        connect=ctx.state.get_provider,
        close=_close_provider,
        needs_canary=(
            ctx.hooks.supervisor.needs_canary
            if ctx.hooks.supervisor is not None
            else _needs_no_canary
        ),
        run_canary=partial(_run_writer_canary, ctx),
        capture_failure=partial(_capture_writer_failure, ctx),
        record_failure=ctx.hooks.record_provider_failure,
        record_success=(
            ctx.hooks.supervisor.record_success
            if ctx.hooks.supervisor is not None
            else _record_no_success
        ),
        clear_session=partial(_clear_provider_session, ctx),
        on_switch=partial(_on_writer_switch, ctx),
        refresh_checkpoint=partial(_refresh_checkpoint_view, ctx),
        stopped=ctx.state.run_registry.stop_flag.is_set,
    )


def _sync_failover_frame(ctx: _ProjectRun) -> None:
    assert ctx.failover is not None
    ctx.frame.provider = ctx.failover.provider
    ctx.frame.provider_id = ctx.failover.provider_id
    ctx.frame.preflight_switches = ctx.failover.switches


def _commit_runtime_operation(
    ctx: _ProjectRun,
    commit: Callable[[Any, str, str], object | None],
) -> None:
    operation = ctx.work.operation
    mutations = ctx.deps.runtime.mutations
    if operation is None or mutations is None:
        return
    try:
        ctx.work.operation = commit(mutations, operation.session_id, operation.run_id)
    except (OSError, ValueError, RuntimeOperationTransitionError):
        ctx.work.operation = None


def _run_writer_phase(ctx: _ProjectRun) -> None:
    ctx.failover = _build_writer_failover(ctx)
    _commit_runtime_operation(
        ctx,
        lambda mutations, session_id, run_id: mutations.mark_writer_running(
            session_id,
            run_id,
            provider_id=ctx.frame.provider_id,
        ),
    )
    try:
        ctx.result = ctx.failover.run(
            task=ctx.agent_task,
            turn_budget=ctx.request.max_turns,
            fresh=ctx.agent_fresh_chat,
            handoff=ctx.frame.handoff,
            checkpoint=CheckpointView(
                prompt=ctx.checkpoint_prompt,
                changed_files=ctx.resumed_changed_files,
                successful_checks=ctx.resumed_successful_checks,
            ),
        )
    finally:
        _sync_failover_frame(ctx)
    _commit_runtime_operation(
        ctx,
        lambda mutations, session_id, run_id: mutations.mark_writer_settled(
            session_id,
            run_id,
            provider_id=ctx.frame.provider_id,
            turns_used=ctx.result.turns,
            stop_reason=ctx.result.stop_reason,
        ),
    )
    ctx.inherited_green = bool(
        ctx.project_context.checkpoint.resumed
        and ctx.work.work_checkpoint is not None
        and not ctx.result.changed
        and not ctx.result.checks_ran
        and ctx.work.evidence.has_successful_checks
    )
    if ctx.inherited_green:
        ctx.result = replace(ctx.result, checks_passed=True)
    checkpoint_changed = bool(
        ctx.work.work_checkpoint is not None
        and ctx.work.work_checkpoint.changed_files
    )
    ctx.task_changed = ctx.result.changed or checkpoint_changed
    ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    collected_changed = change_state(ctx.task_changes)
    ctx.task_changes_dirty = collected_changed is None
    if collected_changed is not None:
        ctx.task_changed = collected_changed
    if ctx.result.stop_reason == "done":
        ctx.hooks.update_checkpoint(
            lambda store, item: store.set_status(item, "ready_for_review")
        )
    ctx.state.set_provider_session(
        ctx.frame.provider_id,
        None if ctx.result.stop_reason == "stopped" else ctx.request.session_id,
    )
    _maybe_consult_consensus_after_writer(ctx)


def _maybe_consult_consensus_after_writer(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    if (
        ctx.deps.agent.run_consensus is None
        or ctx.used_project_audit
        or ctx.result.stop_reason != "done"
        or ctx.result.changed
        or ctx.state.run_registry.stop_flag.is_set()
    ):
        return
    context = render_project_context(
        ctx.frame.conversation.snapshot,
        ctx.verified_facts,
        draft=ctx.result.summary,
        project_map=ctx.project_map,
    )
    try:
        _record_secondary_input_prepared_trace(
            ctx.frame.trace,
            "consensus",
            task=ctx.request.task,
            context=context,
            draft=ctx.result.summary,
        )
        consulted = ctx.deps.agent.run_consensus(
            selected_provider=ctx.frame.provider,
            selected_provider_id=ctx.frame.provider_id,
            task=ctx.request.task,
            context=context,
            draft=ctx.result.summary,
            trace_recorder=ctx.frame.trace,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        ctx.state.set_provider_session(ctx.frame.provider_id, None)
        consulted = None
    if consulted is not None:
        if consulted.degraded:
            ctx.state.set_provider_session(ctx.frame.provider_id, None)
        ctx.result = replace(ctx.result, summary=consulted.answer)


def _render_review_change_brief(ctx: _ProjectRun) -> str:
    return (
        ctx.change_brief.render(audience="reviewer")
        if ctx.change_brief is not None
        else ""
    )


def _refresh_review_project_map(ctx: _ProjectRun) -> str:
    ctx.verified_facts = (
        ctx.deps.persistence.project_facts.render(ctx.project)
        if ctx.deps.persistence.project_facts is not None
        else ""
    )
    ctx.verification_candidates = safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )
    ctx.project_map = safe_project_map(
        ctx.project,
        ctx.verified_facts,
        ctx.request.task,
        verification_candidate_lines(ctx.verification_candidates),
        ignored_paths=ctx.configured_ignored_paths,
        max_chars=ctx.project_map_chars,
    )
    return ctx.project_map


def _close_writer_for_review(ctx: _ProjectRun) -> None:
    if ctx.frame.provider is not None:
        try:
            ctx.frame.provider.close()
        except Exception:
            pass
    ctx.frame.provider = None
    assert ctx.failover is not None
    ctx.failover.provider = None


def _repair_writer(
    ctx: _ProjectRun,
    followup: str,
    checkpoint: CheckpointView,
) -> RunResult:
    assert ctx.failover is not None
    try:
        result = ctx.failover.run(
            task=followup,
            turn_budget=min(ctx.request.max_turns, ctx.deps.review.review_fix_turns),
            fresh=False,
            handoff="",
            checkpoint=checkpoint,
        )
        return result
    finally:
        _sync_failover_frame(ctx)


def _set_checkpoint_status(ctx: _ProjectRun, status: str) -> None:
    ctx.hooks.update_checkpoint(lambda store, item: store.set_status(item, status))


def _emit_review_unavailable(ctx: _ProjectRun) -> None:
    ctx.state.emit(
        {
            "type": "review",
            "session_id": ctx.request.session_id,
            "text": "Unavailable. Continued with one model.",
        }
    )


def _run_review_with_trace(ctx: _ProjectRun, **kwargs):
    try:
        review_impact_map = safe_review_impact_map(
            kwargs.get("project") or ctx.project,
            kwargs.get("changes") if isinstance(kwargs.get("changes"), dict) else {},
        )
    except cancellation.TaskCancelled:
        raise
    except Exception:
        review_impact_map = ""
    record_review_input_prepared_trace(
        ctx.frame.trace,
        task=str(kwargs.get("task") or ""),
        writer_summary=str(kwargs.get("writer_summary") or ""),
        changes=kwargs.get("changes") if isinstance(kwargs.get("changes"), dict) else {},
        recent_log=str(kwargs.get("recent_log") or ""),
        change_brief=str(kwargs.get("change_brief") or ""),
        project_map=str(kwargs.get("project_map") or ""),
        verification_map=str(kwargs.get("verification_map") or ""),
        review_impact_map=review_impact_map,
        execution_evidence=str(kwargs.get("execution_evidence") or ""),
    )
    kwargs["review_impact_map"] = review_impact_map
    kwargs["trace_recorder"] = ctx.frame.trace
    return ctx.deps.review.run(**kwargs)


def _build_review_verification_map(
    ctx: _ProjectRun,
    changes: dict,
    current_project_map: str,
) -> str:
    return safe_verification_map(
        ctx.project,
        changes,
        ctx.work.evidence.successful_checks,
        current_project_map,
        selected_verification_candidate_lines(
            ctx.verification_candidates,
            changed_paths_from_changes(changes),
        ),
    )


def _review_cycle_phase(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    review_coordinator = ReviewCoordinator(ctx.deps.verification.collect_changes)
    ctx.review_cycle = review_coordinator.run_cycle(
        project=ctx.project,
        tracker=ctx.tracker,
        session_id=ctx.request.session_id,
        task=ctx.request.task,
        result=ctx.result,
        task_changed=ctx.task_changed,
        changes=ctx.task_changes,
        changes_dirty=ctx.task_changes_dirty,
        writer_id=ctx.frame.provider_id,
        recent_log="\n".join(ctx.work.recent_events[-ctx.deps.review.review_log_lines :]),
        render_change_brief=partial(_render_review_change_brief, ctx),
        execution_evidence=ctx.work.evidence.render_for_review(),
        successful_checks=ctx.work.evidence.successful_checks,
        checkpoint_prompt=ctx.checkpoint_prompt,
        checks_before_review_followup=(
            ctx.work.evidence.has_successful_checks
            or (not ctx.work.evidence.observed_tool_events and ctx.result.checks_passed)
        ),
        stop_requested=ctx.state.run_registry.stop_flag.is_set,
        refresh_project_map=partial(_refresh_review_project_map, ctx),
        build_verification_map=partial(_build_review_verification_map, ctx),
        run_review=partial(_run_review_with_trace, ctx),
        close_writer_for_review=partial(_close_writer_for_review, ctx),
        repair_writer=partial(_repair_writer, ctx),
        set_checkpoint_status=partial(_set_checkpoint_status, ctx),
        emit_review_unavailable=partial(_emit_review_unavailable, ctx),
    )
    ctx.result = ctx.review_cycle.result
    ctx.task_changed = ctx.review_cycle.task_changed
    ctx.task_changes = ctx.review_cycle.changes
    ctx.task_changes_dirty = ctx.review_cycle.changes_dirty
    if ctx.task_changes is None or ctx.task_changes_dirty:
        ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
    collected_changed = change_state(ctx.task_changes)
    if collected_changed is not None:
        ctx.task_changed = collected_changed


def _enforcement_scope(
    ctx: _ProjectRun,
    changes: dict | None,
    changed: bool,
) -> tuple[bool, tuple[str, ...]]:
    files = tuple(
        str(item.get("path") or "")
        for item in ((changes or {}).get("files") or [])
        if item.get("path")
    )
    if not files and change_state(changes) is None and ctx.work.evidence.changed_files:
        return True, tuple(ctx.work.evidence.changed_files)
    return changed, files


def _completion_evidence(
    ctx: _ProjectRun,
    *,
    changes: object,
    changed: bool,
    scope_files: tuple[str, ...],
    check: object,
    stop: str,
) -> tuple[object, EditIntegrityObservation]:
    assert ctx.completion_engine is not None
    evidence = ctx.completion_engine.evaluate(
        run_id=ctx.frame.run_id,
        task=ctx.request.task,
        changes=changes,
        stop_reason=stop,
        task_changed=changed,
        scope_files=scope_files,
        selected_check=check,
        evidence=ctx.work.evidence,
        analysis_run_payloads=ctx.work.analysis_run_payloads,
        project=ctx.project,
        checkpoint_green=ctx.checkpoint_green,
        verification_forbidden=ctx.verification_forbidden,
    )
    return evidence.decision, evidence.integrity


def _commit_operation_proof(ctx: _ProjectRun, proof: object) -> None:
    if proof is None:
        return
    _commit_runtime_operation(
        ctx,
        lambda mutations, session_id, run_id: mutations.record_completion_proof(
            session_id,
            run_id,
            proof_ref=getattr(proof, "proof_id", ""),
            proof_status=getattr(proof, "status", ""),
            proof_satisfied=getattr(proof, "satisfied", None),
        ),
    )


def _record_completion_evidence(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    if ctx.result.stop_reason == "done" and ctx.task_changed and ctx.files:
        ctx.work.refresh_workspace_state(
            ctx.deps.verification.workspace_revisions,
            ctx.project,
            ignored_paths=ctx.configured_ignored_paths,
        )
    ctx.decision, ctx.integrity = _completion_evidence(
        ctx,
        changes=ctx.task_changes,
        changed=ctx.task_changed,
        scope_files=ctx.files,
        check=ctx.selected_check,
        stop=ctx.result.stop_reason,
    )
    ctx.proof = ctx.decision.proof
    record_completion_proof_trace(ctx.frame.trace, ctx.proof)
    record_edit_integrity_trace(ctx.frame.trace, ctx.integrity)
    _commit_operation_proof(ctx, ctx.proof)


def _prepare_completion_enforcement(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    ctx.task_changed, ctx.files = _enforcement_scope(
        ctx,
        ctx.task_changes,
        ctx.task_changed,
    )
    ctx.verification_candidates = safe_verification_candidates(
        ctx.project,
        ctx.verification_verified_commands,
        ctx.resumed_verification_commands,
        ctx.configured_verification_commands,
        ctx.configured_ignored_paths,
    )
    ctx.selected_check = (
        select_verification_candidate(ctx.verification_candidates, ctx.files)
        if ctx.result.stop_reason == "done" and ctx.task_changed and ctx.files
        else None
    )
    ctx.checkpoint_green = (
        ctx.inherited_green or ctx.review_cycle.inherited_checks_passed
    )
    ctx.verification_forbidden = task_forbids_verification(ctx.request.task)
    ctx.completion_engine = CompletionEngine()


def _maybe_run_completion_repair(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    assert ctx.completion_engine is not None
    remaining_turns = ctx.request.max_turns - ctx.result.turns
    if not (
        ctx.proof is not None
        and not ctx.proof.satisfied
        and not ctx.state.run_registry.stop_flag.is_set()
        and remaining_turns > 0
        and repair_candidate(
            ctx.proof.status,
            ctx.decision.failure_class,
            max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        )
    ):
        return
    projection = project_repair_context(
        proof=ctx.proof.to_payload(),
        failure_class=ctx.decision.failure_class,
        decisive_checks=(
            decisive_failure_fact(
                ctx.selected_check,
                ctx.work.evidence,
                ctx.files,
                root=ctx.project,
            ),
        ),
        changed_files=ctx.files,
        analysis_run_refs=ctx.decision.analysis_run_refs,
    )
    if not projection.admitted:
        ctx.blocked_reason = "repair_context_unavailable"
        return

    ctx.repair_projection = projection
    _commit_runtime_operation(
        ctx,
        lambda mutations, session_id, run_id: mutations.admit_repair_context(
            session_id,
            run_id,
            context_ref=str(projection.to_payload().get("digest") or ""),
        ),
    )
    ctx.hooks.on_event(
        RunEvent.status(
            "[runner] completion proof did not pass; running one bounded repair round."
        )
    )
    _commit_runtime_operation(
        ctx,
        lambda mutations, session_id, run_id: mutations.mark_repair_running(
            session_id,
            run_id,
            provider_id=ctx.frame.provider_id,
        ),
    )

    try:
        repair_result = ctx.failover.run(
            task=COMPLETION_REPAIR_FOLLOWUP,
            turn_budget=remaining_turns,
            fresh=False,
            handoff="",
            checkpoint=_refresh_checkpoint_view(ctx),
        )
    except cancellation.TaskCancelled:
        raise
    except ProviderActionError:
        ctx.blocked_reason = "provider_failure"
        _commit_runtime_operation(
            ctx,
            lambda mutations, session_id, run_id: mutations.mark_repair_settled(
                session_id,
                run_id,
                provider_id=ctx.frame.provider_id,
                stop_reason="",
                blocked_reason="provider_failure",
            ),
        )
    else:
        repair_blocked_reason = ""
        if repair_result.stop_reason not in {"done", "approval", "stopped"}:
            repair_remaining_turns = (
                ctx.request.max_turns - ctx.result.turns - repair_result.turns
            )
            repair_blocked_reason = ctx.completion_engine.blocked_reason(
                proof_status=ctx.proof.status,
                failure_class=ctx.decision.failure_class,
                remaining_turns=repair_remaining_turns,
                repair_rounds=1,
            )
        _commit_runtime_operation(
            ctx,
            lambda mutations, session_id, run_id: mutations.mark_repair_settled(
                session_id,
                run_id,
                provider_id=ctx.frame.provider_id,
                stop_reason=repair_result.stop_reason,
                turns_used=ctx.result.turns + repair_result.turns,
                blocked_reason=repair_blocked_reason,
            ),
        )
        if repair_blocked_reason:
            ctx.blocked_reason = repair_blocked_reason
    finally:
        ctx.repair_projection = None
    ctx.repaired_once = not ctx.blocked_reason
    if ctx.repaired_once:
        _apply_repair_result(ctx, repair_result)


def _apply_repair_result(ctx: _ProjectRun, repair_result: RunResult) -> None:
    assert ctx.result is not None
    turns = ctx.result.turns + repair_result.turns
    if repair_result.stop_reason == "stopped":
        ctx.result = replace(repair_result, turns=turns)
    elif repair_result.stop_reason == "done":
        ctx.task_changes = ctx.deps.verification.collect_changes(ctx.project, ctx.tracker)
        collected = change_state(ctx.task_changes)
        if collected is not None:
            ctx.task_changed = collected
        ctx.task_changed, ctx.files = _enforcement_scope(
            ctx,
            ctx.task_changes,
            ctx.task_changed,
        )
        ctx.verification_candidates = safe_verification_candidates(
            ctx.project,
            ctx.verification_verified_commands,
            ctx.resumed_verification_commands,
            ctx.configured_verification_commands,
            ctx.configured_ignored_paths,
        )
        ctx.selected_check = (
            select_verification_candidate(ctx.verification_candidates, ctx.files)
            if ctx.files
            else None
        )
        ctx.result = RunResult(
            summary=repair_result.summary,
            stop_reason="done",
            turns=turns,
            checks_passed=False,
            changed=ctx.result.changed or repair_result.changed,
            checks_ran=ctx.result.checks_ran or repair_result.checks_ran,
        )
        _record_completion_evidence(ctx)
    else:
        ctx.result = replace(repair_result, turns=turns)


def _settle_blocked_completion(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    assert ctx.completion_engine is not None
    if (
        not ctx.blocked_reason
        and ctx.result.stop_reason == "done"
        and ctx.proof is not None
        and ctx.proof.status in ("failed", "blocked")
    ):
        ctx.blocked_reason = ctx.completion_engine.blocked_reason(
            proof_status=ctx.proof.status,
            failure_class=ctx.decision.failure_class,
            remaining_turns=ctx.request.max_turns - ctx.result.turns,
            repair_rounds=1 if ctx.repaired_once else 0,
        )
    if (
        ctx.blocked_reason
        and ctx.work.operation is not None
        and ctx.work.operation.leaf == LEAF_COMPLETION_PROOF_RECORDED
    ):
        _commit_runtime_operation(
            ctx,
            lambda mutations, session_id, run_id: mutations.mark_completion_blocked(
                session_id,
                run_id,
                reason=ctx.blocked_reason,
            ),
        )


def _apply_completion_verdict(ctx: _ProjectRun) -> None:
    assert ctx.result is not None
    verified = False
    if ctx.blocked_reason and ctx.result.stop_reason in (
        "done",
        "max_turns",
        "no_progress",
        "protocol",
    ):
        ctx.result = blocked_result(ctx.result, ctx.blocked_reason)
    elif ctx.result.stop_reason == "done":
        if ctx.proof is not None:
            verified = ctx.decision.provenance.stance in (
                STANCE_FRESH_PASS,
                STANCE_INHERITED_PASS,
            )
        else:
            verified = bool(ctx.result.checks_passed)
    ctx.result = replace(ctx.result, checks_passed=verified)


def _enforce_completion(ctx: _ProjectRun) -> None:
    _prepare_completion_enforcement(ctx)
    _record_completion_evidence(ctx)
    _maybe_run_completion_repair(ctx)
    _settle_blocked_completion(ctx)
    _apply_completion_verdict(ctx)


def _finalize_project(ctx: _ProjectRun) -> ModeOutcome:
    assert ctx.result is not None
    ctx.receipt = build_task_receipt(
        ctx.task_changes,
        decision=ctx.decision,
        integrity=ctx.integrity,
        checks_passed=ctx.result.checks_passed,
    )
    ctx.hooks.append_ledger(
        lambda ledger: ledger.append_changes_collected(
            ctx.task_changes,
            checks_passed=ctx.result.checks_passed,
            receipt=ctx.receipt.to_dict(),
        )
    )
    facts_write_required = (
        ctx.deps.persistence.project_facts is not None
        and ctx.result.stop_reason == "done"
        and ctx.task_changed
        and ctx.result.checks_passed
        and ctx.receipt.verification.trust == VERIFICATION_TRUST_TRUSTED
        and ctx.work.evidence.has_successful_checks
        and ctx.files
    )
    facts_write_succeeded = not facts_write_required
    if facts_write_required:
        try:
            fact_task = (
                ctx.work.work_checkpoint.original_task
                if ctx.project_context.checkpoint.resumed
                and ctx.work.work_checkpoint is not None
                else ctx.request.task
            )
            facts_write_succeeded = ctx.deps.persistence.project_facts.record_successful_change(
                ctx.project,
                task=fact_task,
                files=ctx.files,
                checks=ctx.work.evidence.successful_checks,
                receipt=ctx.receipt.display.summary,
            )
        except (OSError, ValueError):
            facts_write_succeeded = False
    if facts_write_succeeded and facts_write_required:
        record_project_memory(
            ctx.deps,
            project=ctx.project,
            session_id=ctx.request.session_id,
            task=ctx.request.task,
            files=ctx.files,
            receipt=ctx.receipt.display.summary,
            checks=ctx.work.evidence.successful_checks,
        )
    if ctx.deps.persistence.work_checkpoints is not None and ctx.work.work_checkpoint is not None:
        if ctx.result.stop_reason == "done" and facts_write_succeeded:
            try:
                ctx.deps.persistence.work_checkpoints.delete(ctx.request.session_id)
                ctx.work.work_checkpoint = None
            except OSError:
                pass
        elif ctx.result.stop_reason != "done":
            ctx.hooks.update_checkpoint(
                lambda store, item: store.set_status(
                    item,
                    "interrupted",
                    ctx.result.stop_reason,
                )
            )
    try:
        ctx.tracker.prune_clean()
    except Exception:
        pass
    ctx.frame.conversation.update_snapshot(
        replace(
            ctx.frame.conversation.snapshot,
            provider_id=ctx.frame.provider_id,
            changed_files=ctx.files,
            checks_passed=ctx.result.checks_passed,
            summary=ctx.result.summary,
            blocker="" if ctx.result.stop_reason == "done" else ctx.result.summary,
        )
    )
    changes_payload = None
    if ctx.task_changed and ctx.task_changes and ctx.task_changes.get("ok"):
        changes_payload = {
            "changed_count": ctx.task_changes.get("changed_count", 0),
            "files": ctx.task_changes.get("files", [])[:3],
            "mode": ctx.task_changes.get("mode"),
            "project": ctx.project,
        }
    research_payload = None
    if ctx.research_result is not None:
        research_payload = _research_payload(
            ctx.research_result,
            pipeline_result=ctx.research_pipeline_result,
        )
    event = task_done_event(
        run_id=ctx.frame.run_id,
        session_id=ctx.request.session_id,
        summary=ctx.result.summary,
        stop_reason=ctx.result.stop_reason,
        turns=ctx.result.turns,
        max_turns=ctx.request.max_turns,
        provider=ctx.frame.provider_id,
        mode="hybrid" if ctx.research_result is not None else "agent",
        work=ctx.work,
        changed=ctx.task_changed,
        receipt=ctx.receipt.to_dict(),
        changes=changes_payload,
        research=research_payload,
    )
    return ModeOutcome(
        event,
        research_result=ctx.research_result,
        research_pipeline_result=ctx.research_pipeline_result,
    )


def run_project_mode(
    deps: ProjectCompletionDeps,
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    *,
    config_result: ProjectConfigLoadResult | None = None,
    research_result=None,
    research_pipeline_result=None,
) -> ModeOutcome:
    project = frame.request.project
    if project is None:
        raise RuntimeError("project mode requires a project")
    ctx = _ProjectRun(
        deps=deps,
        frame=frame,
        work=work,
        hooks=hooks,
        project=project,
        config_result=config_result,
        research_result=research_result,
        research_pipeline_result=research_pipeline_result,
    )
    _prepare_project_context(ctx)
    _run_writer_phase(ctx)
    _review_cycle_phase(ctx)
    _enforce_completion(ctx)
    return _finalize_project(ctx)


def handle_project_tool_event(
    deps: ProjectCompletionDeps,
    *,
    event: RunEvent,
    project: str,
    work: RunWork,
    run_id: str,
    update_checkpoint: Callable[
        [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
        None,
    ],
) -> None:
    call = event.call
    outcome = event.outcome
    if call is None or outcome is None:
        return
    name = str(call.name or "")
    if name == "run":
        command = str(call.args.get("command") or "")
        cwd = str(call.args.get("path") or ".")
        ok = bool(outcome.ok and outcome.exit_code == 0)
        try:
            tool_index = int(event.metadata.get("tool_index") or 0)
        except (TypeError, ValueError):
            tool_index = 0
        tool_id = f"{event.turn}:{max(0, tool_index)}"
        if ok and deps.persistence.project_facts is not None:
            try:
                deps.persistence.project_facts.record_success(project, cwd, command)
            except (OSError, ValueError):
                pass
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
    elif name == "edit" and outcome.ok and outcome.changed:
        rel = str(call.args.get("path") or "")
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

    trace = work.trace
    if trace is None or not command:
        return
    try:
        audit = outcome.audit if isinstance(outcome.audit, Mapping) else {}
        # Only real executions become AnalysisRun records. Policy denials,
        # invalid cwd, and command-not-found outcomes carry no timing and
        # must stay out of the execution audit (roadmap: record existing
        # executions, not attempts).
        if not audit.get("command_started_at"):
            return
        managed = outcome.managed_output()
        record = analysis_run_record(
            {
                "run_id": run_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "command": command,
                "cwd": cwd,
                "project": project,
                "exit_code": outcome.exit_code,
                "ok": ok,
                "started_at": audit.get("command_started_at"),
                "finished_at": audit.get("command_finished_at"),
                "duration_ms": audit.get("command_duration_ms"),
                "managed_output": dict(managed) if managed else {},
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
    except Exception:
        return


def record_project_memory(
    deps: ProjectCompletionDeps,
    *,
    project: str,
    session_id: str,
    task: str,
    files: tuple[str, ...],
    receipt: str,
    checks: tuple[object, ...],
) -> None:
    if deps.persistence.knowledge_store is None:
        return
    try:
        brief = KnowledgeBriefBuilder(deps.persistence.knowledge_store).build_for_session(session_id)
        sources = [brief.synthesis_id] if brief.synthesis_id else []
        impl = KnowledgeNote.create(
            type="implementation",
            title=task[:120] or "Project implementation",
            body=(f"Implemented project task.\n\nFiles changed:\n{_bullet_lines(files)}\n\nReceipt:\n{receipt}"),
            tags=["project", "implementation", f"session:{session_id}"],
            sources=sources,
            session_id=session_id,
            project=str(Path(project).expanduser().resolve()),
        )
        deps.persistence.knowledge_store.write_note(impl)
        if checks:
            verification = KnowledgeNote.create(
                type="verification",
                title=f"Verification for {task[:80] or 'project task'}",
                body="Successful checks:\n"
                + _bullet_lines(tuple(f"{item.command} (cwd {item.cwd})" for item in checks)),
                tags=["project", "verification", f"session:{session_id}"],
                sources=[impl.id],
                session_id=session_id,
                project=str(Path(project).expanduser().resolve()),
            )
            deps.persistence.knowledge_store.write_note(verification)
            deps.persistence.knowledge_store.link(impl.id, verification.id, "verifies")
        if brief.synthesis_id:
            deps.persistence.knowledge_store.link(brief.synthesis_id, impl.id, "implements")
    except (OSError, ValueError):
        return
