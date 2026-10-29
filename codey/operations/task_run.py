"""Task run lifecycle.

Thin orchestrator around task_phases: run slot, provider task context,
trace/ledger wiring, and terminal settlement. Phase helpers live in
task_phases (split by concern: lifecycle, ghost, hooks, dispatch,
settlement) so this module stays under the long-file guardrail.
Mode behavior lives in the mode flow modules.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

from codey.app.sibling_probe import bind_provider_handlers
from codey.ghost.work_queue_model import GhostWorkItem
from codey.operations.context import RunFrame, RunWork
from codey.operations.ghost_post_turn import release_work_item, run_ghost_post_turn
from codey.operations.task_phases import (
    build_hooks,
    build_run_work,
    claim_or_route_ghost_work,
    connect_and_build_frame,
    dispatch_run_mode,
    ensure_run_reserved_and_started,
    finish_mode_outcome,
    finish_run_operation,
    ghost_task_deps,
    open_run_ledger,
    open_run_trace,
    project_completion_deps,
    record_route_trace,
    review_flow_deps,
    settle_cancelled_run,
    settle_error_run,
    start_run_operation,
)
from codey.operations.task_state import TaskState, TaskSubmissionStores
from codey.providers import controls as provider_controls
from codey.runtime.core import cancellation
from codey.runtime.core.outcome import OperationOutcome
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.observe.terminalizer import (
    operation_outcome_from_task_done_event,
    task_done_event,
)
from codey.task.kind import resolve_task_kind, trace_mode, ui_mode
from codey.task.model import ResolveShellApproval, TaskSubmission
from codey.workspace.config import ProjectConfigLoadResult, load_project_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskRunDeps:
    state: TaskState
    agent_run: Callable[..., Any]
    collect_changes: Callable[..., Any]
    run_review: Callable[..., Any]
    capture_provider_failure: Callable[..., Any]
    workspace_revisions: Any
    collect_review_changes: Callable[..., Any] | None = None
    run_consensus: Callable[..., Any] | None = None
    run_project_audit: Callable[..., Any] | None = None
    run_research_advisors: Callable[..., Any] | None = None
    project_facts: Any = None
    work_checkpoints: Any = None
    run_ledgers: Any = None
    run_traces: Any = None
    evidence_ledgers: Any = None
    managed_outputs: Any = None
    execution_outputs: Any = None
    knowledge_store: Any = None
    search_factory: Callable[[], object] | None = None
    is_git_repository: Callable[[str | Path], bool] | None = None
    review_fix_turns: int = 12
    review_log_lines: int = 80
    runtime_mutations: RuntimeMutationLine | None = None
    runtime_effects: RuntimeEffectStore | None = None
    connect_provider: Callable[[str], Any] | None = None

    @classmethod
    def from_submission_stores(
        cls,
        *,
        state: TaskState,
        stores: TaskSubmissionStores,
        agent_run: Callable[..., Any],
        collect_changes: Callable[..., Any],
        run_review: Callable[..., Any],
        capture_provider_failure: Callable[..., Any],
        collect_review_changes: Callable[..., Any] | None = None,
        run_consensus: Callable[..., Any] | None = None,
        run_project_audit: Callable[..., Any] | None = None,
        run_research_advisors: Callable[..., Any] | None = None,
        search_factory: Callable[[], object] | None = None,
        is_git_repository: Callable[[str | Path], bool] | None = None,
        review_fix_turns: int = 12,
        review_log_lines: int = 80,
        connect_provider: Callable[[str], Any] | None = None,
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
            run_research_advisors=run_research_advisors,
            workspace_revisions=stores.workspace_revisions,
            project_facts=stores.project_facts,
            work_checkpoints=stores.work_checkpoints,
            run_ledgers=stores.run_ledgers,
            run_traces=stores.run_traces,
            evidence_ledgers=stores.evidence_ledgers,
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
        )


def prepare_submission(state: TaskState, request: TaskSubmission) -> TaskSubmission | None:
    from codey.reviews.reuse import validate_source_run_id

    source = validate_source_run_id(request.review_source_run_id)
    if source:
        kind = str(getattr(request, "intent", "") or "").strip().lower()
        if kind not in ("review", "project"):
            raise ValueError("review source is only supported for review and project intents")
    if request.run_id:
        active_for = getattr(state, "active_run_for", None)
        active = active_for(session_id=request.session_id) if callable(active_for) else state.current_run()
        if active is None:
            reserved = state.reserve_run(
                session_id=request.session_id,
                project=request.project,
                task=request.task,
                provider_id=request.provider_id,
                run_id=request.run_id,
            )
            return request if reserved is not None else None
        # A preset run_id must match this session's active run. Another
        # session may be running independently and is irrelevant here.
        if active.run_id != request.run_id:
            return None
        return request
    reserved = state.reserve_run(
        session_id=request.session_id,
        project=request.project,
        task=request.task,
        provider_id=request.provider_id,
    )
    if reserved is None:
        return None
    return replace(request, run_id=reserved.run_id)


def release_unstarted_submission(state: TaskState, request: TaskSubmission) -> None:
    if not request.run_id:
        return
    try:
        state.release_run(request.run_id)
    except Exception:
        return


def requires_project_writer_lease(request: TaskSubmission, task_kind: str) -> bool:
    """Reserve a writer when this task can modify its attached project."""

    if not request.project:
        return False
    kind = (task_kind or "").strip().lower()
    intent = (request.intent or "auto").strip().lower()
    requested_write = "project.write" in request.requested_capabilities
    if kind in {"project", "hybrid"} and intent != "auto":
        return True
    return requested_write and kind in {"project", "hybrid", "research"}


@dataclass
class _RunSetup:
    """RunSetup phase: reservation, trace, config, and task wiring.

    Everything ``execute_task_run`` needs before touching the workspace.
    Ghost pre-route, provider connect, mode dispatch, and settlement each
    get their own helper below so this orchestrator stays a readable
    phase list instead of a 300-line closure nest."""

    state: TaskState
    request: TaskSubmission
    run_id: str
    session_id: str
    project: str | None
    task: str
    max_turns: int
    continue_task: bool
    provider_id: str
    baseline_task_kind: str
    task_kind: str
    trace: Any
    trace_sink: FailOpenPromptTrace
    project_config_result: ProjectConfigLoadResult
    ghost_deps: Any
    review_deps: Any
    claimed_work_item: GhostWorkItem | None = None
    recovered_resume: bool = False
    previous_cancel_event: Any = None
    writer_acquired: bool = False


@dataclass
class _PhaseWork:
    work: RunWork


def _finish_trace_event(
    trace_sink: FailOpenPromptTrace,
    event: dict[str, object],
    *,
    task_kind: str,
    project: str | None,
    provider_id: str,
) -> None:
    trace_sink.call(
        "finish",
        status=str(event.get("stop_reason") or "done"),
        mode=trace_mode(task_kind, project),
        provider=str(event.get("provider") or provider_id),
    )


def _fail_early_run(
    deps: TaskRunDeps,
    setup: _RunSetup,
    summary_text: str,
    *,
    current_work: RunWork | None = None,
) -> OperationOutcome:
    state = setup.state
    error_event = task_done_event(
        run_id=setup.run_id,
        session_id=setup.session_id,
        summary=summary_text,
        stop_reason="error",
        max_turns=setup.max_turns,
        provider=setup.provider_id,
        mode=ui_mode(setup.task_kind, setup.project),
        work=current_work,
    )
    if current_work is not None:
        finish_run_operation(deps, current_work, error_event)
    _finish_trace_event(
        setup.trace_sink, error_event,
        task_kind=setup.task_kind, project=setup.project, provider_id=setup.provider_id,
    )
    state.finish_run(setup.run_id, error_event)
    run_ghost_post_turn(
        setup.ghost_deps,
        None,
        error_event,
        current_work.claimed_work_item if current_work is not None else setup.claimed_work_item,
        project_text=(setup.project or ""),
    )
    return operation_outcome_from_task_done_event(error_event)


def _release_setup_resources(
    state: TaskState,
    project: str | None,
    previous_cancel_event: Any,
    task_context_started: bool,
    writer_acquired: bool,
) -> None:
    """Release setup-owned resources exactly once (no terminal event here)."""
    if writer_acquired:
        with contextlib.suppress(Exception):
            release = getattr(state, "release_project_writer", None)
            if callable(release):
                release(project)
    if task_context_started:
        with contextlib.suppress(Exception):
            cancellation.set_event(previous_cancel_event)
        with contextlib.suppress(Exception):
            provider_controls.end_task_context()


def _finish_setup_failure(
    state: TaskState,
    request: TaskSubmission,
    run_id: str,
    summary: str,
    *,
    outcome_reason: str,
    task_kind: str,
) -> OperationOutcome:
    """Single setup-failure exit: at most one user-visible terminal event.

    ``finish_run`` releases the slot and emits ``task_done``. When it
    reports ``False`` the run was already settled elsewhere, and when it
    raises the slot is released defensively -- no second ``task_done`` is
    ever emitted as a fallback.
    """
    event = task_done_event(
        run_id=run_id,
        session_id=request.session_id,
        summary=summary,
        stop_reason="error",
        max_turns=request.max_turns,
        provider=request.provider_id,
        mode=ui_mode(task_kind, request.project),
    )
    try:
        finished = state.finish_run(run_id, event)
    except Exception:
        logger.exception("setup failure terminal event failed for run %s", run_id)
        try:
            state.release_run(run_id)
        except Exception:
            logger.exception("setup failure slot release failed for run %s", run_id)
        return OperationOutcome.failed(reason=outcome_reason, summary=summary)
    if not finished:
        logger.warning("setup failure terminal already settled for run %s", run_id)
        try:
            state.release_run(run_id)
        except Exception:
            logger.exception("setup failure slot release failed for run %s", run_id)
    return OperationOutcome.failed(reason=outcome_reason, summary=summary)


def _setup_run_state(deps: TaskRunDeps, request: TaskSubmission) -> tuple[_RunSetup | None, OperationOutcome | None]:
    """Phase 1 -- RunSetup: reserve the slot, open trace, load config."""
    from codey.storage.local_store import StoreCorruption
    from codey.workspace.changes import ProjectWriteBusy

    state = deps.state
    reservation, aborted = ensure_run_reserved_and_started(state, request)
    if reservation is None:
        return None, aborted
    request = reservation.request
    run_id = reservation.run_id
    session_id = request.session_id
    project = request.project
    provider_id = request.provider_id
    baseline_task_kind = resolve_task_kind(request)

    previous_cancel_event: Any = None
    task_context_started = False
    writer_acquired = False
    try:
        if not project and baseline_task_kind == "chat":
            from codey.storage.local_store import DEFAULT_STATE_HOME
            from codey.workspace.session import session_workspace

            home = getattr(deps.workspace_revisions, "state_home", None) or DEFAULT_STATE_HOME
            project = str(session_workspace(home, session_id))
        trace = open_run_trace(deps, session_id, run_id, project, baseline_task_kind, provider_id)
        trace_sink = FailOpenPromptTrace(trace)
        project_config_result = load_project_config(request.project) if request.project else ProjectConfigLoadResult()

        bind_provider_handlers(state)
        provider_controls.begin_task_context(session_id)
        task_context_started = True
        state.run_registry.set_last_provider_failure(None)
        previous_cancel_event = cancellation.set_event(state.run_registry.stop_flag_for(run_id))

        # Single persistent writer: snapshot projects claim cross-process
        # ownership here; a busy project fails fast instead of forking baselines.
        # Only lock contention maps to project_write_busy; IO errors propagate.
        needs_writer = requires_project_writer_lease(request, baseline_task_kind)
        if needs_writer:
            try:
                is_git = deps.is_git_repository
                if project is not None and callable(is_git) and is_git(project):
                    needs_writer = False
            except Exception:
                needs_writer = True
        if needs_writer:
            acquire = getattr(state, "acquire_project_writer", None)
            if callable(acquire):
                try:
                    writer_acquired = bool(acquire(project))
                except ProjectWriteBusy:
                    writer_acquired = False
                if not writer_acquired:
                    _release_setup_resources(
                        state, project, previous_cancel_event,
                        task_context_started, False,
                    )
                    outcome = _finish_setup_failure(
                        state, request, run_id,
                        "another task is writing this project",
                        outcome_reason="project_write_busy",
                        task_kind=baseline_task_kind,
                    )
                    return None, outcome

        review_deps = review_flow_deps(deps)
        ghost_deps = ghost_task_deps(deps, review_deps)
        return _RunSetup(
            state=state,
            request=request,
            run_id=run_id,
            session_id=session_id,
            project=project,
            task=request.task,
            max_turns=request.max_turns,
            continue_task=request.continue_task,
            provider_id=provider_id,
            baseline_task_kind=baseline_task_kind,
            task_kind=baseline_task_kind,
            trace=trace,
            trace_sink=trace_sink,
            project_config_result=project_config_result,
            ghost_deps=ghost_deps,
            review_deps=review_deps,
            previous_cancel_event=previous_cancel_event,
            writer_acquired=writer_acquired,
        ), None
    except StoreCorruption as exc:
        _release_setup_resources(
            state, project, previous_cancel_event,
            task_context_started, writer_acquired,
        )
        outcome = _finish_setup_failure(
            state, request, run_id, "snapshot needs repair",
            outcome_reason="snapshot_corrupt",
            task_kind=baseline_task_kind,
        )
        logger.warning("run setup snapshot corrupt: %s", exc)
        return None, outcome
    except Exception as exc:
        _release_setup_resources(
            state, project, previous_cancel_event,
            task_context_started, writer_acquired,
        )
        summary = f"run setup failed: {type(exc).__name__}"
        outcome = _finish_setup_failure(
            state, request, run_id, summary,
            outcome_reason="setup_failed",
            task_kind=baseline_task_kind,
        )
        logger.exception("run setup failed")
        return None, outcome



def _build_workload(deps: TaskRunDeps, setup: _RunSetup) -> tuple[_PhaseWork | None, OperationOutcome | None]:
    """Phase 2 -- workload: workspace state plus resume recovery."""
    durable = setup.state.durable
    source_run_id = setup.request.previous_run_id or setup.run_id
    if durable is not None:
        try:
            checkpoint = durable.task(source_run_id)
            stored_kind = checkpoint.state.get("task_kind")
            if isinstance(stored_kind, str) and stored_kind:
                setup.task_kind = stored_kind
        except (KeyError, AttributeError, TypeError, ValueError):
            if setup.request.previous_run_id:
                raise RuntimeError("durable recovery task kind is unavailable") from None
    work, _workspace_state = build_run_work(
        deps,
        session_id=setup.session_id,
        run_id=setup.run_id,
        project=setup.project,
        provider_id=setup.provider_id,
        max_turns=setup.max_turns,
        task_kind=setup.task_kind,
        ignored_paths=setup.project_config_result.config.ignored_paths,
        trace=setup.trace,
    )
    if work is None:
        empty_work = RunWork(
            recent_events=[],
            evidence=ExecutionEvidence(
                workspace_revision=_workspace_state.revision,
                workspace_fingerprint=_workspace_state.fingerprint,
            ),
            claimed_work_item=setup.claimed_work_item,
            trace=setup.trace,
            workspace_revision=_workspace_state.revision,
            workspace_fingerprint=_workspace_state.fingerprint,
        )
        return None, _fail_early_run(
            deps, setup, "ERROR: Runtime operation state unavailable.", current_work=empty_work
        )

    # Policy is persisted at entry. Its presence identifies an already-admitted
    # invocation; only the operation driver decides pending execution/recovery.
    if work.operation is not None and work.operation.task_policy:
        if not setup.continue_task:
            setup.request = replace(setup.request, continue_task=True)
            setup.continue_task = True
        setup.recovered_resume = True
    resolved_input = setup.request.resolved_input()
    if isinstance(resolved_input, ResolveShellApproval):
        resolver = getattr(setup.state, "resolve_shell_approval", None)
        payload = resolver(resolved_input.approval_id) if callable(resolver) else None
        if payload is None or payload.get("source_run_id") != setup.run_id:
            return None, _fail_early_run(
                deps, setup, "ERROR: durable shell approval continuation does not match this task.",
                current_work=work,
            )
    return _PhaseWork(work=work), None


def _route_ghost_work(
    setup: _RunSetup, workload: _PhaseWork
) -> OperationOutcome | None:
    """Phase 3 -- GhostLifecycle (pre): claim or route ghost work.

    Returns an early outcome when Stop lands before start; otherwise updates
    ``setup`` in place (request, kinds, claimed item) and emits ``task_start``.
    Post-turn ghost work still runs inside the settlement helpers, so the
    ghost lifecycle bookends the run instead of hiding mid-orchestrator."""
    state = setup.state
    if not setup.recovered_resume:
        try:
            claimed = claim_or_route_ghost_work(
                setup.ghost_deps, setup.request,
                baseline_task_kind=setup.baseline_task_kind, run_id=setup.run_id,
            )
        except cancellation.TaskCancelled:
            state.set_provider_session(setup.provider_id, None)
            release_work_item(
                setup.ghost_deps,
                setup.claimed_work_item,
                run_id=setup.run_id,
                reason="stopped_before_start",
            )
            stopped_event = task_done_event(
                run_id=setup.run_id,
                session_id=setup.session_id,
                summary="",
                stop_reason="stopped",
                max_turns=setup.max_turns,
                provider=setup.provider_id,
                mode=ui_mode(setup.baseline_task_kind, setup.project),
            )
            setup.trace_sink.call(
                "record_mode_selection",
                baseline_mode=trace_mode(setup.baseline_task_kind, setup.project),
                selected_mode=trace_mode(setup.task_kind, setup.project),
                final_mode=trace_mode(setup.task_kind, setup.project),
                source="local_work_item" if setup.claimed_work_item else "baseline",
                reason_code="stopped_before_start",
            )
            _finish_trace_event(
                setup.trace_sink, stopped_event,
                task_kind=setup.task_kind, project=setup.project,
                provider_id=setup.provider_id,
            )
            state.finish_run(setup.run_id, stopped_event)
            return operation_outcome_from_task_done_event(stopped_event)
        setup.request = claimed.request
        setup.task_kind = claimed.task_kind
        setup.claimed_work_item = claimed.claimed_work_item
        workload.work.claimed_work_item = claimed.claimed_work_item
        setup.task = setup.request.task
        setup.continue_task = setup.request.continue_task
    record_route_trace(
        setup.trace_sink,
        request=setup.request,
        baseline_task_kind=setup.baseline_task_kind,
        task_kind=setup.task_kind,
        project=setup.project,
        claimed_work_item=setup.claimed_work_item,
        recovered_resume=setup.recovered_resume,
    )
    state.emit({
        "type": "task_start",
        "run_id": setup.run_id,
        "session_id": setup.session_id,
        "project": setup.project,
        "task": setup.task,
        "mode": ui_mode(setup.task_kind, setup.project),
        "max_turns": setup.max_turns,
        "continue_task": setup.continue_task,
        "provider": setup.provider_id,
        "intent": setup.request.intent,
        "entry_auth": {
            "requested_capabilities": setup.request.requested_capabilities,
            "denied_capabilities": setup.request.denied_capabilities,
            "strict_research": setup.request.strict_research,
            "sources_open_required": setup.request.sources_open_required,
            "project_changes_required": setup.request.project_changes_required,
        },
    })
    return None


def _connect_provider_frame(
    deps: TaskRunDeps, setup: _RunSetup, workload: _PhaseWork, hooks: Any
) -> tuple[RunFrame, Any, str, Any]:
    """Phase 4 -- ProviderConnect: review frames skip providers entirely."""
    state = setup.state
    if setup.task_kind == "review":
        project_text = str(Path(setup.project).expanduser().resolve()) if setup.project else ""
        conversation = state.conversation_for(setup.session_id)
        frame = RunFrame(
            request=setup.request,
            run_id=setup.run_id,
            task_kind=setup.task_kind,
            provider=None,
            provider_id=setup.provider_id,
            project_text=project_text,
            conversation=conversation,
            fresh_chat=False,
            handoff="",
            research_handoff="",
            prior_snapshot=conversation.snapshot,
            recovered_owner_prompt="",
            provider_session_changed=False,
            preflight_tried=set(),
            preflight_switches=0,
            trace=setup.trace,
        )
        return frame, None, setup.provider_id, conversation
    frame, provider, provider_id = connect_and_build_frame(
        deps,
        state,
        setup.request,
        run_id=setup.run_id,
        task_kind=setup.task_kind,
        project=setup.project,
        provider_id=setup.provider_id,
        trace=setup.trace,
        trace_sink=setup.trace_sink,
        hooks=hooks,
        project_config_result=setup.project_config_result,
    )
    return frame, provider, provider_id, frame.conversation


def execute_task_run(deps: TaskRunDeps, request: TaskSubmission) -> OperationOutcome | None:
    setup, early = _setup_run_state(deps, request)
    if setup is None:
        return early
    state = setup.state
    frame: RunFrame | None = None
    provider: Any | None = None
    conversation = None
    workload: _PhaseWork | None = None

    def current_provider_id() -> str:
        return frame.provider_id if frame is not None else setup.provider_id

    def current_provider() -> Any | None:
        return frame.provider if frame is not None else provider

    def finish_trace(event: dict[str, object]) -> None:
        _finish_trace_event(
            setup.trace_sink, event,
            task_kind=setup.task_kind, project=setup.project,
            provider_id=current_provider_id(),
        )

    try:
        workload, early = _build_workload(deps, setup)
        if workload is None:
            return early

        early = _route_ghost_work(setup, workload)
        if early is not None:
            return early

        completion_deps = project_completion_deps(deps)
        open_run_ledger(
            deps, workload.work, setup.request,
            run_id=setup.run_id, task_kind=setup.task_kind, provider_id=setup.provider_id,
            workspace=setup.project,
        )

        hooks = build_hooks(
            deps,
            state,
            workload.work,
            session_id=setup.session_id,
            run_id=setup.run_id,
            project=setup.project,
            max_turns=setup.max_turns,
            project_config_ignored=setup.project_config_result.config.ignored_paths,
            review_log_lines=deps.review_log_lines,
            project_completion_deps=completion_deps,
            current_provider_id=lambda: frame.provider_id if frame is not None else setup.provider_id,
        )

        frame, provider, setup.provider_id, conversation = _connect_provider_frame(
            deps, setup, workload, hooks
        )
        register_provider = getattr(state, "register_active_provider", None)
        if callable(register_provider):
            register_provider(setup.run_id, provider)

        # Phase 5 -- ModeExecution + RunFinalizer (both delegated; ghost
        # post-turn runs inside finish/settle helpers).
        outcome = dispatch_run_mode(
            deps,
            completion_deps,
            setup.review_deps,
            workload.work,
            frame,
            hooks,
            setup.task_kind,
            setup.project_config_result,
        )
        return finish_mode_outcome(
            deps,
            setup.ghost_deps,
            frame,
            workload.work,
            outcome,
            append_ledger=hooks.append_ledger,
            finish_trace=finish_trace,
        )
    except cancellation.TaskCancelled:
        return settle_cancelled_run(
            deps,
            state,
            workload.work if workload is not None else None,
            setup.ghost_deps,
            run_id=setup.run_id,
            session_id=setup.session_id,
            max_turns=setup.max_turns,
            provider_id=current_provider_id(),
            task_kind=setup.task_kind,
            project=setup.project,
            frame=frame,
            finish_trace=finish_trace,
            conversation=conversation,
        )
    except Exception as exc:
        return settle_error_run(
            deps,
            state,
            exc,
            workload.work if workload is not None else None,
            setup.ghost_deps,
            run_id=setup.run_id,
            session_id=setup.session_id,
            max_turns=setup.max_turns,
            provider_id=current_provider_id(),
            task_kind=setup.task_kind,
            project=setup.project,
            frame=frame,
            provider=current_provider(),
            finish_trace=finish_trace,
            conversation=conversation,
        )
    finally:
        cancellation.set_event(setup.previous_cancel_event)
        provider_controls.end_task_context()
        try:
            current_item = current_provider()
            if current_item is not None:
                current_item.close()
        except Exception:
            pass
        unregister_provider = getattr(state, "unregister_active_provider", None)
        if callable(unregister_provider):
            with contextlib.suppress(Exception):
                unregister_provider(setup.run_id, provider)
        if setup.writer_acquired:
            try:
                release = getattr(state, "release_project_writer", None)
                if callable(release):
                    release(setup.project)
            except Exception:
                pass




__all__ = [
    "TaskRunDeps",
    "start_run_operation",
    "execute_task_run",
    "prepare_submission",
    "release_unstarted_submission",
]
