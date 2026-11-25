"""Assemble and admit a task submission for server and headless callers."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from codey.task.model import HandoffRun, ModelHandoffRejected, TaskSubmission

if TYPE_CHECKING:
    from codey.operations.task_run import TaskRunDeps
    from codey.operations.task_state import TaskState
    from codey.providers.base import ChatProvider
    from codey.runtime.core.outcome import OperationOutcome


def run_task_submission(deps: TaskRunDeps, request: TaskSubmission) -> None:
    from codey.operations.task_run import (
        execute_task_run,
        prepare_submission,
        release_unstarted_submission,
    )
    from codey.runtime.write.task_runtime import TaskRuntime

    def prepare(request: TaskSubmission) -> TaskSubmission | None:
        prepared = prepare_submission(deps.state, request)
        if prepared is None:
            return None
        try:
            prepared = _admit_api_selection(deps.state, prepared)
            from codey.operations.task_phases.lifecycle import _ensure_durable_task

            if _ensure_durable_task(deps.state, prepared):
                checkpoint = deps.state.durable.task(prepared.run_id)
                deps.state.durable.start_task(prepared.run_id, state=dict(checkpoint.state))
            return prepared
        except Exception:
            release_unstarted_submission(deps.state, prepared)
            raise

    def execute(request: TaskSubmission) -> OperationOutcome | None:
        if not request.model_selection:
            return execute_task_run(deps, request)
        from codey.runtime.core.api_selection import ApiRunSelection

        selection = ApiRunSelection.from_payload(request.model_selection)

        def connect_selected(provider_id: str) -> ChatProvider:
            if provider_id != selection.connection_id:
                raise ValueError("admitted API connection cannot switch during this run")
            return (deps.connect_provider or deps.state.get_provider)(provider_id)

        return execute_task_run(replace(deps, connect_provider=connect_selected), request)

    runtime = TaskRuntime(
        execute,
        prepare=prepare,
        on_unstarted_failure=partial(release_unstarted_submission, deps.state),
        durable=deps.state.durable,
    )
    runtime.run(request)


def _admit_api_selection(state: TaskState, request: TaskSubmission) -> TaskSubmission:
    from codey.providers.api_connections import capture_selection, connection_for
    from codey.providers.catalog import API_CONNECTIONS
    from codey.runtime.core.api_selection import ApiRunSelection
    durable_payload: dict[str, object] = {}
    for candidate_id in (request.previous_run_id, request.run_id):
        if not candidate_id:
            continue
        try:
            checkpoint = state.durable.task(candidate_id)
        except (KeyError, AttributeError):
            continue
        snapshot = checkpoint.provider_snapshot
        candidate = snapshot.get("model_selection") if isinstance(snapshot, Mapping) else None
        if isinstance(candidate, Mapping):
            durable_payload = dict(candidate)
            break
    payload = durable_payload or request.model_selection
    if isinstance(request.input, HandoffRun) and not payload:
        raise ModelHandoffRejected("Handoff requires an explicit API model selection.")
    if payload:
        selection = ApiRunSelection.from_payload(payload)
        if request.provider_id != selection.connection_id:
            raise ValueError("recovery must use the admitted API connection")
        # Validate availability and credential scope before starting any request.
        connection_for(selection.connection_id).validate_selection(selection)
    elif request.provider_id in API_CONNECTIONS:
        if request.previous_run_id and not durable_payload:
            raise ValueError("original API selection is unavailable; cannot guess a recovery model")
        selection = capture_selection(request.provider_id)
    else:
        if isinstance(request.input, HandoffRun):
            raise ModelHandoffRejected("Handoff requires an explicit API model selection.")
        return request
    if isinstance(request.input, HandoffRun):
        from codey.operations.model_handoff import OBSERVATION_DENIALS, authorize_model_handoff

        if request.continue_task or request.previous_run_id or request.input.observe_only and request.project_changes_required:
            raise ModelHandoffRejected("Handoff must begin a new run with compatible task requirements.")
        authorize_model_handoff(state.durable, request=request.input, target=selection, session_id=request.session_id,
                                run_id=request.run_id, project=request.project)
        if request.input.observe_only:
            request = replace(request, denied_capabilities=tuple(sorted(set(request.denied_capabilities) | OBSERVATION_DENIALS)))
    state.run_registry.bind_api_selection(request.run_id, selection)
    selection_payload: dict[str, object] = selection.to_payload()
    return replace(request, model_selection=selection_payload)
