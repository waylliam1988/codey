"""Task submission wiring for the HTTP server.

``server.py`` keeps HTTP/SSE routing, global STATE, and boot only. Everything
that reserves a run, queues the browser worker, or builds ``TaskRunDeps``
lives here so the task path is importable without the HTTP layer.

Import cost: this module stays light at import time. The agent runner, task
entry, service modules, and workspace scans load inside ``run_task`` on first
use, never on ``import codey.app.task_submit`` (cold start).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from codey.agents.runner import run as agent_run
from codey.automation.browser_worker import BrowserWorkerBusy
from codey.automation.browser_worker import submit as submit_browser_task
from codey.operations.task_state import TaskState
from codey.providers.diagnostics import capture_provider_failure
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
    *,
    get_state: Callable[[], TaskState],
) -> None:
    # Heavy task stack stays lazy: importing this module (and server.py)
    # must not load operations/service modules/research (see test_server_lazy_state).
    from codey.app import consensus_service, review_service
    from codey.app.context import REVIEW_FIX_TURNS, REVIEW_LOG_LINES
    from codey.operations.task_entry import TaskRunDeps, run_task_submission
    from codey.reviews.review_policy import load_review_policy

    state = get_state()
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
        ghost_learning_provider_factory=state.providers.ghost_learning_provider_factory,
        ghost_router_provider_factory=state.providers.ghost_router_provider_factory,
        runtime_mutations=state.runtime_mutations,
        runtime_effects=state.runtime_effects,
    )
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
            )
        )
    finally:
        state = get_state()
        if state.sync_ghost_maintenance:
            state.wait_for_ghost_sleep()
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
    *,
    get_state: Callable[[], TaskState],
    abort_if_stopped: bool = False,
) -> str | None:
    reserved = get_state().reserve_run(
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
            get_state=get_state,
        )
    except Exception:
        get_state().release_run(reserved.run_id)
        raise
    if not accepted:
        get_state().release_run(reserved.run_id)
        raise BrowserWorkerBusy("browser worker busy: queue full")
    get_state().expire_stale_shell_approvals(reserved.run_id)
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
    get_state: Callable[[], TaskState],
    previous_run_id: str = "",
    timeout: float = SHELL_CONTINUATION_IDLE_TIMEOUT,
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
