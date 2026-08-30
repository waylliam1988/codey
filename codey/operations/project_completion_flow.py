from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codey.agents.consensus import render_project_context
from codey.agents.runner import RunResult, task_forbids_verification
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
from codey.runtime.effects import (
    PHASE_COMPLETION_PROOF_RECORDED,
    mark_completion_blocked,
    mark_completion_proof_recorded,
    mark_repair_context_admitted,
    mark_repair_running,
    mark_repair_settled,
    mark_writer_running,
    mark_writer_settled,
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
class ProjectCompletionDeps:
    state: Any
    agent_run: Callable
    collect_changes: Callable
    run_review: Callable
    capture_provider_failure: Callable[..., ProviderFailure]
    commit_run_operation: Callable[[RunWork, Callable], None]
    run_consensus: Callable | None = None
    run_project_audit: Callable | None = None
    project_facts: ProjectFactsStore | None = None
    work_checkpoints: WorkCheckpointStore | None = None
    managed_outputs: ManagedOutputStore | None = None
    knowledge_store: KnowledgeStore | None = None
    is_git_repository: Callable[[str | Path], bool] = _default_is_git_repository
    review_fix_turns: int = 12
    review_log_lines: int = 80


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
    if deps.managed_outputs is None:
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
            store=deps.managed_outputs,
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
    "COMPLETION_REPAIR_FOLLOWUP",
    "MAX_COMPLETION_REPAIR_ROUNDS",
    "ProjectCompletionDeps",
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
    state = deps.state
    request = frame.request
    project = request.project
    if project is None:
        raise RuntimeError("project mode requires a project")
    if work.ledger is not None:
        work.record_agent_events_in_ledger = True
    context_builder = ProjectTaskContextBuilder(
        project_facts=deps.project_facts,
        work_checkpoints=deps.work_checkpoints,
        knowledge_store=deps.knowledge_store,
        config_result=config_result,
    )
    project_context = context_builder.build(
        project=project,
        task=request.task,
        session_id=request.session_id,
        run_id=frame.run_id,
        continue_task=request.continue_task,
        provider_session_changed=frame.provider_session_changed,
    )
    verified_facts = project_context.verified_facts
    verification_verified_commands = project_context.verification_verified_commands
    verification_candidates = project_context.verification_candidates
    resumed_verification_commands = project_context.resumed_verification_commands
    configured_verification_commands = project_context.configured_verification_commands
    configured_ignored_paths = project_context.configured_ignored_paths
    project_map_chars = project_context.project_map_chars
    project_map = project_context.project_map
    work.work_checkpoint = project_context.checkpoint.item
    checkpoint_prompt = project_context.checkpoint.prompt
    resumed_changed_files = project_context.checkpoint.changed_files
    resumed_successful_checks = project_context.checkpoint.successful_checks
    work.evidence.seed_checks(project_context.checkpoint.seed_checks)
    agent_task = request.task
    change_brief: ChangeBrief | None = None
    agent_fresh_chat = frame.fresh_chat
    has_user_files = project_has_user_files(project)
    used_project_audit = False
    if deps.run_consensus is not None and not has_user_files:
        context = render_project_context(
            frame.conversation.snapshot,
            verified_facts,
            project_map=project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                frame.trace,
                "consensus",
                task=request.task,
                context=context,
            )
            planned = deps.run_consensus(
                selected_provider=frame.provider,
                selected_provider_id=frame.provider_id,
                task=request.task,
                context=context,
                plan=True,
                draft_first=True,
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            state.set_provider_session(frame.provider_id, None)
            agent_fresh_chat = True
            planned = None
        if planned is not None:
            change_brief = new_project_change_brief(request.task, planned.answer)
            agent_task = change_brief.apply_to_task(request.task)
            agent_fresh_chat = True
    elif deps.run_project_audit is not None and has_user_files:
        context = render_project_context(
            frame.conversation.snapshot,
            verified_facts,
            project_map=project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                frame.trace,
                "project_audit",
                task=request.task,
                context=context,
            )
            reports = deps.run_project_audit(
                project=project,
                selected_provider=frame.provider,
                selected_provider_id=frame.provider_id,
                task=request.task,
                context=context,
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reports = ()
        if reports:
            change_brief = project_audit_change_brief(request.task, reports)
            agent_task = change_brief.apply_to_task(request.task)
            used_project_audit = True
    key = str(Path(project).expanduser().resolve())
    tracker = state.change_tracker_for(
        key,
        persistent=not deps.is_git_repository(key),
    )
    tried_writers = set(frame.preflight_tried)
    # One-shot holder for the repair phase: run_one_writer_attempt passes
    # the admitted projection into agent.run(), whose ContextSource
    # machinery renders it and binds the admission row to the outbound
    # send epoch. It is empty for every normal writer attempt.
    repair_projection: RepairContextProjection | None = None

    def refresh_checkpoint_view() -> CheckpointView:
        nonlocal checkpoint_prompt
        nonlocal resumed_changed_files
        nonlocal resumed_successful_checks
        refreshed = context_builder.refresh_checkpoint(work.work_checkpoint)
        if refreshed.item is None:
            checkpoint_prompt = ""
            resumed_changed_files = ()
            resumed_successful_checks = ()
        else:
            work.work_checkpoint = refreshed.item
            checkpoint_prompt = refreshed.prompt
            resumed_changed_files = refreshed.changed_files
            resumed_successful_checks = refreshed.successful_checks
        if refreshed.workspace_changed:
            work.evidence.invalidate_checks()
        return CheckpointView(
            prompt=checkpoint_prompt,
            changed_files=resumed_changed_files,
            successful_checks=resumed_successful_checks,
        )

    def run_one_writer_attempt(
        spec: WriterAttempt,
        note_turn: Callable[[int], None],
    ) -> RunResult:
        def on_writer_event(event: RunEvent) -> None:
            note_turn(event.turn)
            hooks.on_event(event)

        return deps.agent_run(
            spec.provider,
            Path(project),
            spec.task,
            max_turns=spec.remaining_turns,
            on_event=on_writer_event,
            on_shell_request=hooks.on_shell_request,
            stop_flag=state.stop_flag,
            fresh_chat=spec.fresh_chat,
            strict_fresh_chat=spec.strict_fresh_chat,
            change_tracker=tracker,
            conversation=frame.conversation,
            provider_id=spec.provider_id,
            handoff=spec.handoff,
            project_facts=verified_facts,
            research_context=project_context.research_context,
            project_map=project_map,
            project_config_warnings=project_context.project_config_warnings,
            work_checkpoint=spec.checkpoint.prompt,
            verification_candidates=verification_candidates,
            verification_candidate_loader=lambda: safe_verification_candidates(
                project,
                verification_verified_commands,
                resumed_verification_commands,
                configured_verification_commands,
                configured_ignored_paths,
            ),
            verification_changed_files=spec.checkpoint.changed_files,
            verification_successful_checks=(spec.checkpoint.successful_checks),
            ghost_directive="",
            ghost_continuity="",
            completion_repair_context=(repair_projection.prompt_text if repair_projection is not None else ""),
            completion_repair_context_payload=(
                repair_projection.to_payload() if repair_projection is not None else None
            ),
            permission_profile="coding_writer",
            tool_fns=managed_tool_fns(
                deps,
                session_id=request.session_id,
                run_id=frame.run_id,
            ),
            trace_recorder=frame.trace,
        )

    def select_next_writer(excluded: set[str]) -> str | None:
        mode = _writer_failover_mode(frame.task_kind)
        preference = preferred_provider_for(config_result.config, mode) if config_result is not None else ""
        ranked_order = rank_providers(
            hooks.provider_failover_order(),
            mode=mode,
            preferred=preference,
        )
        if hooks.supervisor is not None:
            return hooks.supervisor.select(
                "",
                ranked_order,
                excluded=excluded,
            )
        return next(
            (item for item in ranked_order if item not in excluded),
            None,
        )

    def capture_writer_failure(
        pid: str,
        action: str,
        error: BaseException,
    ) -> ProviderFailure:
        return deps.capture_provider_failure(
            model=PROVIDER_LABELS.get(pid, pid),
            action=action,
            page=None,
            error=error,
        )

    def on_writer_switch(next_provider_id: str) -> None:
        previous_provider_id = frame.provider_id
        state.switch_run_provider(frame.run_id, next_provider_id)
        hooks.append_ledger(
            lambda ledger: ledger.append(
                "provider_switched",
                from_provider=previous_provider_id,
                to_provider=next_provider_id,
                phase="writer_failover",
                reason="provider_failure",
            )
        )
        FailOpenPromptTrace(hooks.trace).call(
            "record_fallback",
            from_provider=previous_provider_id,
            to_provider=next_provider_id,
            phase="writer_failover",
            reason_code="provider_failure",
        )
        FailOpenPromptTrace(hooks.trace).call(
            "record_policy_decision",
            _provider_fallback_policy_decision(
                from_provider=previous_provider_id,
                to_provider=next_provider_id,
                phase="writer_failover",
            ),
        )
        frame.conversation.update_snapshot(
            replace(
                frame.conversation.snapshot,
                provider_id=next_provider_id,
                blocker="",
            )
        )

    failover = WriterFailoverRunner(
        provider=frame.provider,
        provider_id=frame.provider_id,
        switches=frame.preflight_switches,
        tried=tried_writers,
        attempt=run_one_writer_attempt,
        select_next=select_next_writer,
        connect=state.get_provider,
        close=lambda item: item.close(),
        needs_canary=(hooks.supervisor.needs_canary if hooks.supervisor is not None else (lambda _pid: False)),
        run_canary=(lambda pid, item: run_half_open_canary(pid, item, hooks.supervisor)),
        capture_failure=capture_writer_failure,
        record_failure=hooks.record_provider_failure,
        record_success=(hooks.supervisor.record_success if hooks.supervisor is not None else (lambda _pid: None)),
        clear_session=lambda pid: state.set_provider_session(pid, None),
        on_switch=on_writer_switch,
        refresh_checkpoint=refresh_checkpoint_view,
        stopped=state.stop_flag.is_set,
    )

    deps.commit_run_operation(
        work,
        lambda state: mark_writer_running(state, provider_id=frame.provider_id),
    )

    try:
        result = failover.run(
            task=agent_task,
            turn_budget=request.max_turns,
            fresh=agent_fresh_chat,
            handoff=frame.handoff,
            checkpoint=CheckpointView(
                prompt=checkpoint_prompt,
                changed_files=resumed_changed_files,
                successful_checks=resumed_successful_checks,
            ),
        )
    finally:
        frame.provider = failover.provider
        frame.provider_id = failover.provider_id
        frame.preflight_switches = failover.switches
    deps.commit_run_operation(
        work,
        lambda state: mark_writer_settled(
            state,
            provider_id=frame.provider_id,
            turns_used=result.turns,
            stop_reason=result.stop_reason,
        ),
    )
    # Narrow checkpoint-resume green inheritance: the workspace did not
    # change and nothing new ran, so prior green checks still cover it.
    # The receipt stays green, but the completion proof now records this
    # explicitly as stance=inherited_pass / source=checkpoint -- never as
    # this round's clean verification fact (0.4.13 provenance debt).
    inherited_green = bool(
        project_context.checkpoint.resumed
        and work.work_checkpoint is not None
        and not result.changed
        and not result.checks_ran
        and work.evidence.has_successful_checks
    )
    if inherited_green:
        result = replace(result, checks_passed=True)
    checkpoint_changed = bool(work.work_checkpoint is not None and work.work_checkpoint.changed_files)
    task_changed = result.changed or checkpoint_changed
    task_changes = deps.collect_changes(project, tracker)
    collected_changed = change_state(task_changes)
    task_changes_dirty = collected_changed is None
    if collected_changed is not None:
        task_changed = collected_changed
    if result.stop_reason == "done":
        hooks.update_checkpoint(lambda store, item: store.set_status(item, "ready_for_review"))
    state.set_provider_session(
        frame.provider_id,
        None if result.stop_reason == "stopped" else request.session_id,
    )
    if (
        deps.run_consensus is not None
        and not used_project_audit
        and result.stop_reason == "done"
        and not result.changed
        and not state.stop_flag.is_set()
    ):
        context = render_project_context(
            frame.conversation.snapshot,
            verified_facts,
            draft=result.summary,
            project_map=project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                frame.trace,
                "consensus",
                task=request.task,
                context=context,
                draft=result.summary,
            )
            consulted = deps.run_consensus(
                selected_provider=frame.provider,
                selected_provider_id=frame.provider_id,
                task=request.task,
                context=context,
                draft=result.summary,
                trace_recorder=frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            state.set_provider_session(frame.provider_id, None)
            consulted = None
        if consulted is not None:
            if consulted.degraded:
                state.set_provider_session(frame.provider_id, None)
            result = replace(result, summary=consulted.answer)
    review_coordinator = ReviewCoordinator(deps.collect_changes)

    def render_review_change_brief() -> str:
        return change_brief.render(audience="reviewer") if change_brief is not None else ""

    def refresh_review_project_map() -> str:
        nonlocal verified_facts
        nonlocal project_map
        nonlocal verification_candidates
        verified_facts = deps.project_facts.render(project) if deps.project_facts is not None else ""
        verification_candidates = safe_verification_candidates(
            project,
            verification_verified_commands,
            resumed_verification_commands,
            configured_verification_commands,
            configured_ignored_paths,
        )
        project_map = safe_project_map(
            project,
            verified_facts,
            request.task,
            verification_candidate_lines(verification_candidates),
            ignored_paths=configured_ignored_paths,
            max_chars=project_map_chars,
        )
        return project_map

    def close_writer_for_review() -> None:
        if frame.provider is not None:
            try:
                frame.provider.close()
            except Exception:
                pass
        frame.provider = None
        failover.provider = None

    def repair_writer(
        followup: str,
        checkpoint: CheckpointView,
    ) -> RunResult:
        try:
            return failover.run(
                task=followup,
                turn_budget=min(request.max_turns, deps.review_fix_turns),
                fresh=False,
                handoff="",
                checkpoint=checkpoint,
            )
        finally:
            frame.provider = failover.provider
            frame.provider_id = failover.provider_id
            frame.preflight_switches = failover.switches

    def set_checkpoint_status(status: str) -> None:
        hooks.update_checkpoint(
            lambda store, item: store.set_status(
                item,
                status,
            )
        )

    def emit_review_unavailable() -> None:
        state.emit(
            {
                "type": "review",
                "session_id": request.session_id,
                "text": "Unavailable. Continued with one model.",
            }
        )

    def run_review_with_trace(**kwargs):
        try:
            review_impact_map = safe_review_impact_map(
                kwargs.get("project") or project,
                kwargs.get("changes") if isinstance(kwargs.get("changes"), dict) else {},
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            review_impact_map = ""
        record_review_input_prepared_trace(
            frame.trace,
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
        kwargs["trace_recorder"] = frame.trace
        return deps.run_review(**kwargs)

    review_cycle = review_coordinator.run_cycle(
        project=project,
        tracker=tracker,
        session_id=request.session_id,
        task=request.task,
        result=result,
        task_changed=task_changed,
        changes=task_changes,
        changes_dirty=task_changes_dirty,
        writer_id=frame.provider_id,
        recent_log="\n".join(work.recent_events[-deps.review_log_lines :]),
        render_change_brief=render_review_change_brief,
        execution_evidence=work.evidence.render_for_review(),
        successful_checks=work.evidence.successful_checks,
        checkpoint_prompt=checkpoint_prompt,
        checks_before_review_followup=(
            work.evidence.has_successful_checks or (not work.evidence.observed_tool_events and result.checks_passed)
        ),
        stop_requested=state.stop_flag.is_set,
        refresh_project_map=refresh_review_project_map,
        build_verification_map=lambda changes, current_project_map: safe_verification_map(
            project,
            changes,
            work.evidence.successful_checks,
            current_project_map,
            selected_verification_candidate_lines(
                verification_candidates,
                changed_paths_from_changes(changes),
            ),
        ),
        run_review=run_review_with_trace,
        close_writer_for_review=close_writer_for_review,
        repair_writer=repair_writer,
        set_checkpoint_status=set_checkpoint_status,
        emit_review_unavailable=emit_review_unavailable,
    )
    result = review_cycle.result
    task_changed = review_cycle.task_changed
    task_changes = review_cycle.changes
    task_changes_dirty = review_cycle.changes_dirty
    if task_changes is None or task_changes_dirty:
        task_changes = deps.collect_changes(project, tracker)
    collected_changed = change_state(task_changes)
    if collected_changed is not None:
        task_changed = collected_changed

    def enforcement_scope(
        changes: dict | None,
        changed: bool,
    ) -> tuple[bool, tuple[str, ...]]:
        files = tuple(str(item.get("path") or "") for item in ((changes or {}).get("files") or []) if item.get("path"))
        if not files and change_state(changes) is None and work.evidence.changed_files:
            # Changes collection produced no usable verdict while real
            # edits were observed locally: scope enforcement from the
            # observed edits instead of letting an edited run slip past
            # enforcement as "unchanged". A measured net-empty diff --
            # the model reverted its own edit -- is a verdict, so it
            # keeps the run out of scope with an honest unchanged
            # receipt.
            return True, tuple(work.evidence.changed_files)
        return changed, files

    task_changed, files = enforcement_scope(task_changes, task_changed)
    verification_candidates = safe_verification_candidates(
        project,
        verification_verified_commands,
        resumed_verification_commands,
        configured_verification_commands,
        configured_ignored_paths,
    )
    # --- Verified Completion Enforcement (0.4.13) --------------------
    # The first decision point where local facts constrain done: build
    # the completion proof, admit at most one bounded repair context for
    # an observed product failure, then let the FINAL proof drive
    # receipt, ledger, project facts, and the user-visible event.
    selected_check = (
        select_verification_candidate(verification_candidates, files)
        if result.stop_reason == "done" and task_changed and files
        else None
    )
    checkpoint_green = inherited_green or review_cycle.inherited_checks_passed
    verification_forbidden = task_forbids_verification(request.task)

    # The decision inputs are passed explicitly at every call site: the
    # repair round re-collects changes and re-selects the candidate, and
    # the integrity observation must read the exact same snapshot of
    # changes/files/check as the decision it qualifies -- never a diff
    # captured before the repair.
    completion_engine = CompletionEngine()

    def completion_evidence(
        *,
        changes: object,
        changed: bool,
        scope_files: tuple[str, ...],
        check: object,
        stop: str,
    ) -> tuple[object, EditIntegrityObservation]:
        evidence = completion_engine.evaluate(
            run_id=frame.run_id,
            task=request.task,
            changes=changes,
            stop_reason=stop,
            task_changed=changed,
            scope_files=scope_files,
            selected_check=check,
            evidence=work.evidence,
            analysis_run_payloads=work.analysis_run_payloads,
            project=project,
            checkpoint_green=checkpoint_green,
            verification_forbidden=verification_forbidden,
        )
        return evidence.decision, evidence.integrity

    def commit_operation_proof(proof: object) -> None:
        # Refs/status only; the proof body stays in the run trace. The
        # facts pass through uncoerced: the strict helper validates them
        # and refuses anything it cannot record honestly.
        if proof is None:
            return
        deps.commit_run_operation(
            work,
            lambda state: mark_completion_proof_recorded(
                state,
                proof_ref=getattr(proof, "proof_id", ""),
                proof_status=getattr(proof, "status", ""),
                proof_satisfied=getattr(proof, "satisfied", None),
            ),
        )

    decision, integrity = completion_evidence(
        changes=task_changes,
        changed=task_changed,
        scope_files=files,
        check=selected_check,
        stop=result.stop_reason,
    )
    proof = decision.proof
    record_completion_proof_trace(frame.trace, proof)
    record_edit_integrity_trace(frame.trace, integrity)
    commit_operation_proof(proof)

    blocked_reason = ""
    repaired_once = False
    remaining_turns = request.max_turns - result.turns
    if (
        proof is not None
        and not proof.satisfied
        and not state.stop_flag.is_set()
        and remaining_turns > 0
        and repair_candidate(
            proof.status,
            decision.failure_class,
            max_repair_rounds=MAX_COMPLETION_REPAIR_ROUNDS,
        )
    ):
        projection = project_repair_context(
            proof=proof.to_payload(),
            failure_class=decision.failure_class,
            decisive_checks=(
                decisive_failure_fact(
                    selected_check,
                    work.evidence,
                    files,
                    root=project,
                ),
            ),
            changed_files=files,
            analysis_run_refs=decision.analysis_run_refs,
        )
        if not projection.admitted:
            blocked_reason = "repair_context_unavailable"
        else:
            repair_projection = projection
            deps.commit_run_operation(
                work,
                lambda state: mark_repair_context_admitted(
                    state,
                    context_ref=str(projection.to_payload().get("digest") or ""),
                ),
            )
            hooks.on_event(RunEvent.status("[runner] completion proof did not pass; running one bounded repair round."))
            deps.commit_run_operation(
                work,
                lambda state: mark_repair_running(
                    state,
                    provider_id=frame.provider_id,
                ),
            )
            try:
                repair_result = failover.run(
                    task=COMPLETION_REPAIR_FOLLOWUP,
                    turn_budget=remaining_turns,
                    fresh=False,
                    handoff="",
                    checkpoint=refresh_checkpoint_view(),
                )
            except cancellation.TaskCancelled:
                raise
            except ProviderActionError:
                blocked_reason = "provider_failure"
                deps.commit_run_operation(
                    work,
                    lambda state: mark_repair_settled(
                        state,
                        provider_id=frame.provider_id,
                        stop_reason="",
                        blocked_reason="provider_failure",
                    ),
                )
            else:
                repair_blocked_reason = ""
                if repair_result.stop_reason not in {"done", "approval", "stopped"}:
                    repair_remaining_turns = request.max_turns - result.turns - repair_result.turns
                    repair_blocked_reason = completion_engine.blocked_reason(
                        proof_status=proof.status,
                        failure_class=decision.failure_class,
                        remaining_turns=repair_remaining_turns,
                        repair_rounds=1,
                    )
                deps.commit_run_operation(
                    work,
                    lambda state: mark_repair_settled(
                        state,
                        provider_id=frame.provider_id,
                        stop_reason=repair_result.stop_reason,
                        turns_used=result.turns + repair_result.turns,
                        blocked_reason=repair_blocked_reason,
                    ),
                )
                if repair_blocked_reason:
                    blocked_reason = repair_blocked_reason
            finally:
                repair_projection = None
            repaired_once = not blocked_reason
            if repaired_once:
                # The repair is bounded by the shared remaining turn
                # budget, so the sum can never exceed max_turns.
                turns = result.turns + repair_result.turns
                if repair_result.stop_reason == "stopped":
                    result = replace(repair_result, turns=turns)
                elif repair_result.stop_reason == "done":
                    # Re-collect post-repair facts; the new proof decides.
                    task_changes = deps.collect_changes(project, tracker)
                    collected = change_state(task_changes)
                    if collected is not None:
                        task_changed = collected
                    task_changed, files = enforcement_scope(
                        task_changes,
                        task_changed,
                    )
                    verification_candidates = safe_verification_candidates(
                        project,
                        verification_verified_commands,
                        resumed_verification_commands,
                        configured_verification_commands,
                        configured_ignored_paths,
                    )
                    selected_check = select_verification_candidate(verification_candidates, files) if files else None
                    result = RunResult(
                        summary=repair_result.summary,
                        stop_reason="done",
                        turns=turns,
                        checks_passed=False,
                        changed=result.changed or repair_result.changed,
                        checks_ran=result.checks_ran or repair_result.checks_ran,
                    )
                    decision, integrity = completion_evidence(
                        changes=task_changes,
                        changed=task_changed,
                        scope_files=files,
                        check=selected_check,
                        stop=result.stop_reason,
                    )
                    proof = decision.proof
                    record_completion_proof_trace(frame.trace, proof)
                    record_edit_integrity_trace(frame.trace, integrity)
                    commit_operation_proof(proof)
                else:
                    result = replace(repair_result, turns=turns)

    if (
        not blocked_reason
        and result.stop_reason == "done"
        and proof is not None
        and proof.status in ("failed", "blocked")
    ):
        # A done claim backed by a failed or unverifiable proof must
        # never pass as done. complete_with_limitations (docs-only,
        # inherited green) stays an allowed -- but honest -- done. The
        # repair_rounds fact comes from this run's operation state
        # position: a round actually ran only when the repair arm
        # settled without a provider failure.
        blocked_reason = completion_engine.blocked_reason(
            proof_status=proof.status,
            failure_class=decision.failure_class,
            remaining_turns=request.max_turns - result.turns,
            repair_rounds=1 if repaired_once else 0,
        )
    if blocked_reason and work.operation is not None and work.operation.phase == PHASE_COMPLETION_PROOF_RECORDED:
        # The verdict lands on the durable counter at the decision
        # point; provider-failure and stop verdicts are already
        # carried by their own settled phases.
        deps.commit_run_operation(
            work,
            lambda state: mark_completion_blocked(state, reason=blocked_reason),
        )

    verified = False
    if blocked_reason and result.stop_reason in (
        "done",
        "max_turns",
        "no_progress",
        "protocol",
    ):
        # Explicit stop conditions win; everything else becomes an
        # honest blocked result instead of a claimed done.
        result = blocked_result(result, blocked_reason)
    elif result.stop_reason == "done":
        if proof is not None:
            verified = decision.provenance.stance in (
                STANCE_FRESH_PASS,
                STANCE_INHERITED_PASS,
            )
        else:
            # Out of enforcement scope (no changed files): keep the
            # pre-enforcement flag semantics.
            verified = bool(result.checks_passed)
    result = replace(result, checks_passed=verified)

    receipt = build_task_receipt(
        task_changes,
        decision=decision,
        integrity=integrity,
        checks_passed=result.checks_passed,
    )
    hooks.append_ledger(
        lambda ledger: ledger.append_changes_collected(
            task_changes,
            checks_passed=result.checks_passed,
            receipt=receipt.to_dict(),
        )
    )
    facts_write_required = (
        deps.project_facts is not None
        and result.stop_reason == "done"
        and task_changed
        and result.checks_passed
        and receipt.verification.trust == VERIFICATION_TRUST_TRUSTED
        and work.evidence.has_successful_checks
        and files
    )
    facts_write_succeeded = not facts_write_required
    if facts_write_required:
        try:
            fact_task = (
                work.work_checkpoint.original_task
                if project_context.checkpoint.resumed and work.work_checkpoint is not None
                else request.task
            )
            facts_write_succeeded = deps.project_facts.record_successful_change(
                project,
                task=fact_task,
                files=files,
                checks=work.evidence.successful_checks,
                receipt=receipt.display.summary,
            )
        except (OSError, ValueError):
            facts_write_succeeded = False
    if facts_write_succeeded and facts_write_required:
        record_project_memory(
            deps,
            project=project,
            session_id=request.session_id,
            task=request.task,
            files=files,
            receipt=receipt.display.summary,
            checks=work.evidence.successful_checks,
        )
    if deps.work_checkpoints is not None and work.work_checkpoint is not None:
        if result.stop_reason == "done" and facts_write_succeeded:
            try:
                deps.work_checkpoints.delete(request.session_id)
                work.work_checkpoint = None
            except OSError:
                pass
        elif result.stop_reason != "done":
            hooks.update_checkpoint(
                lambda store, item: store.set_status(
                    item,
                    "interrupted",
                    result.stop_reason,
                )
            )
    # Terminal state for this run: now -- and only now -- drop snapshot
    # baselines whose files are back to their original content. UI
    # polling during a run never prunes.
    try:
        tracker.prune_clean()
    except Exception:
        pass
    frame.conversation.update_snapshot(
        replace(
            frame.conversation.snapshot,
            provider_id=frame.provider_id,
            changed_files=files,
            checks_passed=result.checks_passed,
            summary=result.summary,
            blocker="" if result.stop_reason == "done" else result.summary,
        )
    )
    changes_payload = None
    if task_changed and task_changes and task_changes.get("ok"):
        changes_payload = {
            "changed_count": task_changes.get("changed_count", 0),
            "files": task_changes.get("files", [])[:3],
            "mode": task_changes.get("mode"),
            "project": project,
        }
    research_payload = None
    if research_result is not None:
        research_payload = _research_payload(
            research_result,
            pipeline_result=research_pipeline_result,
        )
    event = task_done_event(
        run_id=frame.run_id,
        session_id=request.session_id,
        summary=result.summary,
        stop_reason=result.stop_reason,
        turns=result.turns,
        max_turns=request.max_turns,
        provider=frame.provider_id,
        mode="hybrid" if research_result is not None else "agent",
        work=work,
        changed=task_changed,
        receipt=receipt.to_dict(),
        changes=changes_payload,
        research=research_payload,
    )
    return ModeOutcome(
        event,
        research_result=research_result,
        research_pipeline_result=research_pipeline_result,
    )


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
        if ok and deps.project_facts is not None:
            try:
                deps.project_facts.record_success(project, cwd, command)
            except (OSError, ValueError):
                pass
        update_checkpoint(
            lambda store, item: store.record_run(
                item,
                command=command,
                cwd=cwd,
                ok=ok,
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
    if deps.knowledge_store is None:
        return
    try:
        brief = KnowledgeBriefBuilder(deps.knowledge_store).build_for_session(session_id)
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
        deps.knowledge_store.write_note(impl)
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
            deps.knowledge_store.write_note(verification)
            deps.knowledge_store.link(impl.id, verification.id, "verifies")
        if brief.synthesis_id:
            deps.knowledge_store.link(brief.synthesis_id, impl.id, "implements")
    except (OSError, ValueError):
        return
