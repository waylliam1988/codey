"""Task submission wiring for the HTTP server.

``server.py`` keeps HTTP/SSE routing, global STATE, and boot only. Everything
that reserves a run, queues the browser worker, or builds ``TaskRunDeps``
lives here so the task path is importable without the HTTP layer.

Import cost: this module stays light at import time. The agent runner, task
entry, service modules, and workspace scans load inside ``run_task`` on first
use, never on ``import codey.app.task_submit`` (cold start).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable

from codey.automation.browser_worker import BrowserWorkerBusy
from codey.automation.browser_worker import submit as submit_browser_task
from codey.operations.project_adapter import run as agent_run
from codey.operations.task_state import TaskSubmissionState
from codey.providers.diagnostics import capture_provider_failure
from codey.reviews.review_policy import load_review_policy
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes, is_git_repository

SHELL_CONTINUATION_IDLE_TIMEOUT = 15.0


def run_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    run_id: str = "",
    requested_capabilities: tuple[str, ...] = (),
    strict_research: bool = False,
    sources_open_required: bool = False,
    project_changes_required: bool = False,
    denied_capabilities: tuple[str, ...] = (),
    previous_run_id: str = "",
    initial_shell_results: tuple[dict[str, object], ...] = (),
    *,
    get_state: Callable[[], TaskSubmissionState],
    review_policy: str | None = None,
) -> None:
    # Heavy task stack stays lazy: importing this module (and server.py)
    # must not load operations/service modules/research (see test_server_lazy_state).
    # review_policy itself stays import-light (os + env names only).
    # The lazy imports and get_state() live inside the guarded block so any
    # init-phase failure (import, state, policy, deps) releases a preset
    # reservation instead of pinning busy forever.
    try:
        from codey.app import consensus_service, review_service
        from codey.app.context import REVIEW_FIX_TURNS, REVIEW_LOG_LINES
        from codey.operations.task_entry import run_task_submission
        from codey.operations.task_run import TaskRunDeps

        state = get_state()
        if review_policy is None:
            review_policy = load_review_policy()
        deps = TaskRunDeps(
            state=state,
            agent_run=agent_run,
            collect_changes=collect_changes,
            run_review=lambda **kwargs: review_service.run_review(
                state, review_policy=review_policy, **kwargs
            ),
            capture_provider_failure=capture_provider_failure,
            run_consensus=lambda **kwargs: consensus_service.run_consensus(state, **kwargs),
            run_project_audit=lambda **kwargs: consensus_service.run_project_audit(state, **kwargs),
            run_research_advisors=lambda **kwargs: consensus_service.run_research_advisors(state, **kwargs),
            project_facts=state.project_facts,
            work_checkpoints=state.work_checkpoints,
            workspace_revisions=state.workspace_revisions,
            run_ledgers=state.run_ledgers,
            run_traces=state.run_traces,
            evidence_ledgers=state.evidence_ledgers,
            managed_outputs=state.managed_outputs,
            knowledge_store=state.knowledge_store,
            is_git_repository=is_git_repository,
            review_fix_turns=REVIEW_FIX_TURNS,
            review_log_lines=REVIEW_LOG_LINES,
            runtime_mutations=state.runtime_mutations,
            runtime_effects=state.runtime_effects,
        )
    except Exception:
        # Init-phase failure happens before TaskRuntime owns the slot: release
        # a preset reservation so the worker exception cannot pin busy forever.
        # Post-entry failures stay owned by TaskRuntime (release is idempotent).
        # get_state() itself is guarded too: a broken accessor still surfaces
        # the original error, just without a release to aim at.
        if run_id:
            with contextlib.suppress(Exception):
                get_state().release_run(run_id)
        raise
    try:
        run_task_submission(
            deps,
            TaskSubmission(
                session_id=session_id,
                project=project,
                task=task,
                max_turns=max_turns,
                continue_task=continue_task,
                provider_id=provider_id,
                intent=intent,
                run_id=run_id,
                requested_capabilities=tuple(requested_capabilities or ()),
                strict_research=bool(strict_research),
                sources_open_required=bool(sources_open_required),
                project_changes_required=bool(project_changes_required),
                denied_capabilities=tuple(denied_capabilities or ()),
                previous_run_id=str(previous_run_id or ""),
                initial_shell_results=tuple(initial_shell_results or ()),
            )
        )
    finally:
        state = get_state()
        supervisor = state.self_repair
        if supervisor is not None:
            supervisor.kick_if_idle(state.is_busy)


def submit_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    requested_capabilities: tuple[str, ...] = (),
    strict_research: bool = False,
    sources_open_required: bool = False,
    project_changes_required: bool = False,
    denied_capabilities: tuple[str, ...] = (),
    previous_run_id: str = "",
    initial_shell_results: tuple[dict[str, object], ...] = (),
    *,
    get_state: Callable[[], TaskSubmissionState],
    abort_if_stopped: bool = False,
) -> str | None:
    # Fail fast on config error before taking the slot; the validated value is
    # passed through so the worker never re-reads the environment (no race).
    # The state object is captured once and handed to the worker as-is, so
    # init-phase cleanup never depends on the accessor working a second time.
    review_policy = load_review_policy()
    state = get_state()
    reserved = state.reserve_run(
        session_id=session_id,
        project=project,
        task=task,
        provider_id=provider_id,
        abort_if_stopped=abort_if_stopped,
    )
    if reserved is None:
        return None
    try:
        accepted = submit_browser_task(
            run_task,
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
            reserved.run_id,
            requested_capabilities=tuple(requested_capabilities or ()),
            strict_research=bool(strict_research),
            sources_open_required=bool(sources_open_required),
            project_changes_required=bool(project_changes_required),
            denied_capabilities=tuple(denied_capabilities or ()),
            previous_run_id=str(previous_run_id or ""),
            initial_shell_results=tuple(initial_shell_results or ()),
            get_state=lambda: state,
            review_policy=review_policy,
        )
    except Exception:
        state.release_run(reserved.run_id)
        raise
    if not accepted:
        state.release_run(reserved.run_id)
        raise BrowserWorkerBusy("browser worker busy: queue full")
    state.expire_stale_shell_approvals(reserved.run_id)
    return reserved.run_id


def submit_task_after_slot_release(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    *,
    get_state: Callable[[], TaskSubmissionState],
    previous_run_id: str = "",
    timeout: float = SHELL_CONTINUATION_IDLE_TIMEOUT,
    initial_shell_results: tuple[dict[str, object], ...] = (),
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        # Fast path; the authoritative guard is the atomic
        # abort_if_stopped reservation below, which closes the race where a
        # Stop lands between this peek and the reserve.
        state = get_state()
        if state.run_registry.stop_flag.is_set():
            return None
        active = state.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        run_id = submit_task(
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
            get_state=get_state,
            abort_if_stopped=True,
            previous_run_id=previous_run_id,
            initial_shell_results=tuple(initial_shell_results or ()),
        )
        if run_id is not None:
            return run_id
        active = state.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        if time.monotonic() >= deadline:
            return None
        remaining = max(0.0, deadline - time.monotonic())
        state.run_registry.wait_for_slot(remaining)


__all__ = [
    "SHELL_CONTINUATION_IDLE_TIMEOUT",
    "run_task",
    "submit_task",
    "submit_task_after_slot_release",
]
