"""Task submission wiring for the HTTP server.

``server.py`` keeps HTTP/SSE routing and explicitly attached context only. Everything
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
    from codey.app.run_registry import RunSnapshot
    from codey.runtime.core.api_selection import ApiRunSelection

from codey.automation.browser_worker import submit as submit_browser_task
from codey.durable.errors import CommitConflict
from codey.durable.model import TASK_TERMINAL_STATUSES, TaskStatus, thaw_json
from codey.operations.project_adapter import run as agent_run
from codey.operations.task_state import TaskSubmissionState
from codey.providers.diagnostics import capture_provider_failure
from codey.providers.model_preferences import ModelDisabledError
from codey.providers.snapshot import capture_provider_selection, provider_snapshot
from codey.reviews.review_policy import load_review_policy
from codey.task.kind import resolve_task_kind_values
from codey.task.model import EditUserMessage, ResolveShellApproval, ResumeRun, StartTask, TaskInput, TaskSubmission
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
    input: TaskInput | None = None,
    *,
    get_state: Callable[[], TaskSubmissionState],
    review_policy: str | None = None,
    review_source_run_id: str = "",
    model_selection: ApiRunSelection | None = None,
    worker_key: str = "",
    attempt_token: str = "",
) -> None:
    # Heavy task stack stays lazy: importing this module (and server.py)
    # must not load operations/service modules/research (see test_server_lazy_state).
    # review_policy itself stays import-light (os + env names only).
    # The lazy imports and get_state() live inside the guarded block so any
    # init-phase failure (import, state, policy, deps) releases a preset
    # reservation instead of pinning busy forever.
    state = get_state()
    if run_id:
        try:
            if state.durable.task(run_id).attempt_token != attempt_token:
                return
        except KeyError:
            return
    with state.durable.execution(run_id, attempt_token) if run_id else contextlib.nullcontext():
        # Browser-worker dispatches carry a pre-admitted durable task ID.  Direct
        # callers (including the operation-level entry point) deliberately leave
        # ``run_id`` empty so the lifecycle can reserve the run and assign its
        # identity before durable task admission.  Only gate the former here;
        # task_run remains the single place that starts an unassigned run.
        if run_id:
            try:
                checkpoint = state.durable.task(run_id)
                from codey.durable.model import TaskStatus

                if checkpoint.status != TaskStatus.RUNNING:
                    return
                if checkpoint.phase == "dispatching":
                    runtime_state = dict(checkpoint.state)
                    control = getattr(state, "control_service", None)
                    if control is not None:
                        runtime_state["execution_runtime"] = {"instance_id": control.instance_id, "release_id": control.release_id}
                    state.durable.start_task(run_id, state=runtime_state)
            except (CommitConflict, KeyError, ValueError):
                return
        try:
            from codey.app.task_services import build_task_deps
            from codey.operations.task_entry import run_task_submission

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
                    checkpoint = state.durable.task(run_id)
                    if checkpoint.phase == "provider_prepared":
                        state.durable.fail_task(run_id, "task initialization failed", state=dict(checkpoint.state))
                    state.release_run(run_id)
            raise
        try:
            if input is not None:
                submission = TaskSubmission.from_input(
                    session_id=session_id,
                    project=project,
                    max_turns=max_turns,
                    provider_id=provider_id,
                    input=input,
                    intent=intent,
                    run_id=run_id,
                    requested_capabilities=tuple(requested_capabilities or ()),
                    strict_research=strict_research,
                    sources_open_required=sources_open_required,
                    project_changes_required=project_changes_required,
                    denied_capabilities=tuple(denied_capabilities or ()),
                    review_source_run_id=review_source_run_id,
                    model_selection=model_selection.to_payload() if model_selection is not None else {},
                )
            else:
                submission = TaskSubmission(
                    session_id=session_id,
                    project=project,
                    task=task,
                    max_turns=max_turns,
                    continue_task=continue_task,
                    provider_id=provider_id,
                    intent=intent,
                    run_id=run_id,
                    requested_capabilities=tuple(requested_capabilities or ()),
                    strict_research=strict_research,
                    sources_open_required=sources_open_required,
                    project_changes_required=project_changes_required,
                    denied_capabilities=tuple(denied_capabilities or ()),
                    previous_run_id=(previous_run_id or ""),
                    initial_shell_results=tuple(initial_shell_results or ()),
                    review_source_run_id=review_source_run_id,
                    model_selection=model_selection.to_payload() if model_selection is not None else {},
                    input=input,
                )
            run_task_submission(
                deps,
                submission,
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
    input: TaskInput | None = None,
) -> str | None:
    # Fail fast on config error before taking the slot; the validated value is
    # passed through so the worker never re-reads the environment (no race).
    # The state object is captured once and handed to the worker as-is, so
    # init-phase cleanup never depends on the accessor working a second time.
    review_policy = load_review_policy()
    state = get_state()
    service = getattr(state, "control_service", None)
    if service is not None and service.draining:
        return None
    admitted_selection = capture_provider_selection(provider_id, model_selection)
    shell_tool_results: tuple[dict[str, object], ...] = ()
    if isinstance(input, ResolveShellApproval):
        resolved = state.resolve_shell_approval(input.approval_id)
        if resolved is None:
            return None
        call_id = str(resolved.get("call_id") or "")
        if call_id:
            shell_tool_results = ({
                "call_id": call_id,
                "content": str(resolved.get("model_text") or ""),
                "is_error": resolved.get("ok") is not True,
            },)
    # Settings saves use this same lock: disabling and admission cannot cross.
    with state._shell_spawn_gate, state.lock:
        if service is not None and service.draining:
            return None
        if not state.providers.model_preferences.allows(provider_id, admitted_selection.model_id if admitted_selection else ""):
            raise ModelDisabledError("Selected model is disabled. Choose an enabled model in Settings.")
        if isinstance(input, ResolveShellApproval):
            try:
                approval = state.durable.approval(input.approval_id)
            except (KeyError, OSError, TypeError, ValueError):
                return None
            if str(approval.status) not in {"execution_settled", "denied"}:
                return None
            if str(getattr(approval, "continuation_status", "waiting_for_result")) in {
                "cancelled", "failed", "unavailable",
            }:
                return None
        existing_reservation = any(row.run_id == run_id for row in state.run_registry.active_runs()) if run_id else False
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
        reserved_run_id = str(reserved.run_id)
        if admitted_selection is not None:
            state.run_registry.bind_api_selection(reserved.run_id, admitted_selection)
    try:
        state.durable.admit_input(
            conversation_id=session_id,
            request_id=reserved.run_id,
            task_id=reserved.run_id,
            text=task,
            provider_snapshot=provider_snapshot(
                provider_id,
                admitted_selection,
                provider_session_id=state.providers.sessions_snapshot().get(provider_id, ""),
            ),
            task_state={
                "project": project,
                "task": task,
                "max_turns": max_turns,
                "continue_task": continue_task,
                "provider_id": provider_id,
                "intent": intent,
                "task_kind": resolve_task_kind_values(project, intent),
                "requested_capabilities": list(requested_capabilities),
                "strict_research": strict_research,
                "sources_open_required": sources_open_required,
                "project_changes_required": project_changes_required,
                "denied_capabilities": list(denied_capabilities),
                "previous_run_id": previous_run_id,
                "initial_shell_results": list(initial_shell_results),
                "review_source_run_id": review_source_run_id,
                "model_selection": admitted_selection.to_payload() if admitted_selection is not None else {},
                "input": _task_input_payload(input),
            },
            tool_results=shell_tool_results,
        )
    except Exception:
        if not existing_reservation:
            state.release_run(reserved.run_id)
        raise
    try:
        if not _claim_admitted_task(state, reserved, existing_reservation=existing_reservation):
            return reserved_run_id
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
            strict_research=strict_research,
            sources_open_required=sources_open_required,
            project_changes_required=project_changes_required,
            denied_capabilities=tuple(denied_capabilities or ()),
            previous_run_id=(previous_run_id or ""),
            initial_shell_results=tuple(initial_shell_results or ()),
            input=input,
            get_state=lambda: state,
            review_policy=review_policy,
            review_source_run_id=review_source_run_id,
            model_selection=admitted_selection,
            worker_key=session_id,
            attempt_token=state.durable.task(reserved.run_id).attempt_token,
        )
    except Exception:
        with contextlib.suppress(Exception):
            state.durable.unclaim_task(reserved.run_id)
        state.durable_scheduler.retry(reserved.run_id)
        state.release_run(reserved.run_id)
        raise
    if not accepted:
        state.durable.unclaim_task(reserved.run_id)
        state.durable_scheduler.retry(reserved.run_id)
        return reserved_run_id
    state.expire_stale_shell_approvals(reserved.run_id)
    run_id_value: str = reserved_run_id
    return run_id_value


def _claim_admitted_task(state: TaskSubmissionState, reserved: RunSnapshot, *, existing_reservation: bool) -> bool:
    """Claim once; repeated HTTP confirmation must not dispatch another worker."""
    checkpoint = state.durable.task(reserved.run_id)
    if checkpoint.status in TASK_TERMINAL_STATUSES:
        if not existing_reservation:
            state.release_run(reserved.run_id)
        return False
    # Stop can land after reservation but before the checkpoint exists.
    if reserved.stop_event.is_set():
        state.durable_scheduler.abort_task(reserved.run_id, reason="user stopped during admission")
        state.release_run(reserved.run_id)
        return False
    if checkpoint.status != TaskStatus.PENDING:
        if not existing_reservation:
            state.release_run(reserved.run_id)
        return False
    try:
        state.durable.claim_task(reserved.run_id)
    except (CommitConflict, ValueError):
        if state.durable.task(reserved.run_id).status == TaskStatus.PENDING:
            raise
        if not existing_reservation:
            state.release_run(reserved.run_id)
        return False
    state.durable_scheduler.claim(reserved.run_id)
    return True


def _task_input_payload(input: TaskInput | None) -> dict[str, object] | None:
    if isinstance(input, EditUserMessage):
        return {"kind": "edit_user_message", "entry_id": input.entry_id, "text": input.text,
                "expected_revision": input.expected_revision}
    if isinstance(input, StartTask):
        return {"kind": "start_task", "text": input.text}
    if isinstance(input, ResumeRun):
        return {"kind": "resume_run", "source_run_id": input.source_run_id, "instruction": input.instruction}
    if isinstance(input, ResolveShellApproval):
        return {"kind": "resolve_shell_approval", "approval_id": input.approval_id}
    return None


def dispatch_durable_task(checkpoint: object, *, state: TaskSubmissionState) -> bool:
    """Resume one durable checkpoint after admission or process restart."""
    with state._shell_spawn_gate:
        return _dispatch_durable_task(checkpoint, state=state)


def _dispatch_durable_task(checkpoint: object, *, state: TaskSubmissionState) -> bool:
    from codey.durable.model import TaskCheckpoint

    if not isinstance(checkpoint, TaskCheckpoint):
        raise TypeError("durable dispatcher requires a TaskCheckpoint")
    control_service = getattr(state, "control_service", None)
    if control_service is not None and not control_service.permits_dispatch(checkpoint):
        return False
    stored = thaw_json(checkpoint.state)
    if not isinstance(stored, dict):
        raise TypeError("task checkpoint state must be an object")
    session_id = checkpoint.conversation_id
    task_id = checkpoint.task_id
    task = str(stored.get("task") or "")
    project_raw = stored.get("project")
    project = str(project_raw) if project_raw is not None else None
    provider_id = str(stored.get("provider_id") or checkpoint.provider_snapshot.get("provider_id") or "")
    if not task or not provider_id:
        raise ValueError("accepted durable task is missing dispatch identity")
    active = state.active_run_for(session_id=session_id)
    if active is None:
        reserved = state.reserve_run(
            session_id=session_id,
            project=project,
            task=task,
            provider_id=provider_id,
            run_id=task_id,
        )
        if reserved is None:
            return False
    elif active.run_id != task_id:
        return False
    model_payload = stored.get("model_selection")
    model_selection = None
    if isinstance(model_payload, dict) and model_payload:
        from codey.runtime.core.api_selection import ApiRunSelection

        model_selection = ApiRunSelection.from_payload(model_payload)
    input_value = _task_input_from_payload(stored.get("resume_input", stored.get("input")))
    max_turns_raw = stored.get("max_turns")
    max_turns = max_turns_raw if type(max_turns_raw) is int else 8
    try:
        accepted = bool(
            submit_browser_task(
                run_task,
                session_id,
                project,
                task,
                max_turns,
                bool(stored.get("continue_task")),
                provider_id,
                str(stored.get("intent") or "auto"),
                task_id,
                requested_capabilities=_string_tuple(stored.get("requested_capabilities")),
                strict_research=bool(stored.get("strict_research")),
                sources_open_required=bool(stored.get("sources_open_required")),
                project_changes_required=bool(stored.get("project_changes_required")),
                denied_capabilities=_string_tuple(stored.get("denied_capabilities")),
                previous_run_id=str(stored.get("previous_run_id") or ""),
                initial_shell_results=_mapping_tuple(stored.get("initial_shell_results")),
                input=input_value,
                get_state=lambda: state,
                review_source_run_id=str(stored.get("review_source_run_id") or ""),
                model_selection=model_selection,
                worker_key=session_id,
                attempt_token=checkpoint.attempt_token,
            )
        )
        if not accepted:
            state.release_run(task_id)
        return accepted
    except Exception:
        state.release_run(task_id)
        raise


def _task_input_from_payload(value: object) -> TaskInput | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "")
    if kind == "edit_user_message":
        return EditUserMessage(value["entry_id"], value["text"], value["expected_revision"])
    if kind == "start_task":
        return StartTask(str(value.get("text") or ""))
    if kind == "resume_run":
        return ResumeRun(str(value.get("source_run_id") or ""), str(value.get("instruction") or ""))
    if kind == "resolve_shell_approval":
        return ResolveShellApproval(str(value.get("approval_id") or ""))
    raise ValueError(f"unknown durable task input kind: {kind}")


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _mapping_tuple(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


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
    input: TaskInput | None = None,
    run_id: str = "",
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        # Fast path; the authoritative guard is the atomic
        # abort_if_stopped reservation below, which closes the race where a
        # Stop lands between this peek and the reserve.
        state = get_state()
        stop_flag = state.run_registry.stop_flag_for(previous_run_id) if previous_run_id else state.run_registry.stop_flag
        if stop_flag.is_set():
            return None
        active = state.active_run_for(session_id=session_id)
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        submitted_run_id = submit_task(
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
            input=input,
            run_id=run_id,
            model_selection=model_selection,
        )
        if submitted_run_id is not None:
            return submitted_run_id
        active = state.active_run_for(session_id=session_id)
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
