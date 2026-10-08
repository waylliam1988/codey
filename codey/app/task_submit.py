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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.runtime.core.api_selection import ApiRunSelection

from codey.automation.browser_worker import BrowserWorkerBusy
from codey.automation.browser_worker import submit as submit_browser_task
from codey.operations.project_adapter import run as agent_run
from codey.operations.task_state import TaskSubmissionState
from codey.providers.diagnostics import capture_provider_failure
from codey.providers.model_preferences import ModelDisabledError
from codey.reviews.review_policy import load_review_policy
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes

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
    review_source_run_id: str = "",
    model_selection: ApiRunSelection | None = None,
) -> None:
    # Heavy task stack stays lazy: importing this module (and server.py)
    # must not load operations/service modules/research (see test_server_lazy_state).
    # review_policy itself stays import-light (os + env names only).
    # The lazy imports and get_state() live inside the guarded block so any
    # init-phase failure (import, state, policy, deps) releases a preset
    # reservation instead of pinning busy forever.
    try:
        from codey.app.task_services import build_task_deps
        from codey.operations.task_entry import run_task_submission

        state = get_state()
        if review_policy is None:
            review_policy = load_review_policy()
        deps = build_task_deps(
            state,
            agent_run=agent_run,
            collect_changes=collect_changes,
            review_policy=review_policy,
            capture_provider_failure=capture_provider_failure,
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
                review_source_run_id=review_source_run_id,
                model_selection=model_selection.to_payload() if model_selection is not None else {},
            )
        )
    finally:
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
    review_source_run_id: str = "",
    model_selection: ApiRunSelection | None = None,
    run_id: str = "",
) -> str | None:
    # Fail fast on config error before taking the slot; the validated value is
    # passed through so the worker never re-reads the environment (no race).
    # The state object is captured once and handed to the worker as-is, so
    # init-phase cleanup never depends on the accessor working a second time.
    review_policy = load_review_policy()
    state = get_state()
    # Settings saves use this same lock: disabling and admission cannot cross.
    with state.lock:
        if not state.providers.model_preferences.allows(provider_id, model_selection.model_id if model_selection else ""):
            raise ModelDisabledError("Selected model is disabled. Choose an enabled model in Settings.")
        reserved = state.reserve_run(
            session_id=session_id,
            project=project,
            task=task,
            provider_id=provider_id,
            abort_if_stopped=abort_if_stopped,
            run_id=run_id,
        )
        if reserved is None:
            return None
        if model_selection is not None:
            state.run_registry.bind_api_selection(reserved.run_id, model_selection)
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
            review_source_run_id=review_source_run_id,
            model_selection=model_selection,
        )
    except Exception:
        state.release_run(reserved.run_id)
        raise
    if not accepted:
        state.release_run(reserved.run_id)
        raise BrowserWorkerBusy("browser worker busy: queue full")
    state.expire_stale_shell_approvals(reserved.run_id)
    run_id_value: str = reserved.run_id
    return run_id_value


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
    model_selection: ApiRunSelection | None = None,
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
            model_selection=model_selection,
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
