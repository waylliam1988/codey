"""Second-model consensus and project-audit runs.

``task_submit`` builds ``TaskRunDeps.run_consensus`` / ``run_project_audit``
from these. Import-light by design (see ``test_server_lazy_state``): nothing
here may load the browser stack at import time.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from codey.agents.consensus import (
    ConsensusAdvice,
    ConsensusResult,
    ConsensusSender,
)
from codey.agents.consensus import (
    run_consensus as run_consensus_core,
)
from codey.app import provider_services as providers
from codey.operations.task_state import TaskState, TaskSubmissionState
from codey.providers.base import ChatProvider
from codey.providers.catalog import API_CONNECTIONS, PROVIDER_LABELS
from codey.utils.object_fields import object_fields, sequence_items

if TYPE_CHECKING:
    from codey.policies.task_policy import TaskPolicy


def connect_consensus_provider(selected_provider: object, provider_id: str) -> ChatProvider:
    """Use an already-open sibling tab while a Writer provider is active."""

    if provider_id in API_CONNECTIONS:
        return providers.connect_existing_provider(provider_id)
    session: object = getattr(selected_provider, "session", None)
    owner_page: object = getattr(session, "page", None)
    if owner_page is not None:
        helper = providers.borrow_open_provider(provider_id, owner_page)
        if helper is None:
            raise RuntimeError(
                f"{providers.review_label(provider_id)} tab is not open in this browser context"
            )
        return helper
    return providers.connect_existing_provider(provider_id)


def _scoped_advisor(ctx: TaskState, selected_provider: object, provider_id: str, leases: ExitStack) -> ChatProvider:
    with ExitStack() as candidate:
        if provider_id in API_CONNECTIONS:
            from codey.providers.api_connections import capture_selection, open_selection

            preferences: object = ctx.providers.model_preferences.snapshot()
            if (not object_fields(preferences) or not object_fields(sources := preferences.get("sources"))
                    or not object_fields(choices := sources.get(provider_id))):
                raise ValueError("Invalid model preferences")
            models = choices.get("models")
            if (choices.get("enabled") is not True or not sequence_items(models) or not models
                    or not isinstance(model := models[0], str)):
                raise ValueError("Selected model is disabled")
            selection = capture_selection(provider_id, {"model": model})
            candidate.enter_context(providers.use_model(ctx, provider_id, selection.model_id))
            provider = open_selection(selection)
        else:
            candidate.enter_context(providers.use_model(ctx, provider_id))
            provider = connect_consensus_provider(selected_provider, provider_id)
        leases.enter_context(candidate.pop_all())
        return provider


def run_consensus(
    ctx: TaskState,
    *,
    selected_provider: ConsensusSender,
    selected_provider_id: str,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    trace_recorder: object | None = None,
) -> ConsensusResult | None:
    with ExitStack() as leases:
        return run_consensus_core(
            selected_provider=selected_provider,
            selected_provider_id=selected_provider_id,
            task=task,
            provider_ids=tuple(PROVIDER_LABELS),
            provider_labels=PROVIDER_LABELS,
            availability=lambda: providers.provider_availability(ctx),
            connect_existing=lambda provider_id: _scoped_advisor(ctx, selected_provider, provider_id, leases),
            clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
            context=context,
            draft=draft,
            plan=plan,
            draft_first=draft_first,
            owner_prompt=owner_prompt,
            trace_recorder=trace_recorder,
        )


def run_project_audit(
    ctx: TaskSubmissionState,
    *,
    parent_policy: TaskPolicy,
    project: str | Path,
    selected_provider: object = None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    from codey.operations.project_audit_advisor import (
        run_project_audit as run_project_audit_core,
    )

    with ExitStack() as leases:
        return run_project_audit_core(
            parent_policy=parent_policy,
            durable=ctx.durable, runtime_mutations=ctx.runtime_mutations,
            managed_outputs=ctx.task_submission_stores.managed_outputs,
            project=project,
            selected_provider_id=selected_provider_id,
            task=task,
            provider_ids=tuple(PROVIDER_LABELS),
            provider_labels=PROVIDER_LABELS,
            availability=lambda: providers.provider_availability(ctx),
            connect_existing=lambda provider_id: _scoped_advisor(ctx, selected_provider, provider_id, leases),
            clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
            context=context,
            trace_recorder=trace_recorder,
        )


__all__ = [
    "connect_consensus_provider",
    "run_consensus",
    "run_project_audit",
]
