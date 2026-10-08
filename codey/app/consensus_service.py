"""Second-model consensus, audit, and research-advisor runs.

``task_submit`` builds ``TaskRunDeps.run_consensus`` / ``run_project_audit`` /
``run_research_advisors`` from these. Import-light by design (see
``test_server_lazy_state``): the research core stays function-local, nothing
here may load the browser stack at import time.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.research.advisors import EvidencePack

from codey.agents.consensus import (
    ConsensusAdvice,
    ConsensusResult,
)
from codey.agents.consensus import (
    run_consensus as run_consensus_core,
)
from codey.app import provider_services as providers
from codey.operations.project_audit_advisor import (
    run_project_audit as run_project_audit_core,
)
from codey.operations.task_state import TaskState
from codey.providers.catalog import API_CONNECTIONS, PROVIDER_LABELS


def connect_consensus_provider(selected_provider: Any, provider_id: str) -> Any:
    """Use an already-open sibling tab while a Writer provider is active."""

    if provider_id in API_CONNECTIONS:
        return providers.connect_existing_provider(provider_id)
    owner_page = getattr(getattr(selected_provider, "session", None), "page", None)
    if owner_page is not None:
        helper = providers.borrow_open_provider(provider_id, owner_page)
        if helper is None:
            raise RuntimeError(
                f"{providers.review_label(provider_id)} tab is not open in this browser context"
            )
        return helper
    return providers.connect_existing_provider(provider_id)


def _scoped_advisor(ctx: TaskState, selected_provider: Any, provider_id: str, leases: ExitStack) -> Any:
    with ExitStack() as candidate:
        if provider_id in API_CONNECTIONS:
            from codey.providers.api_connections import capture_selection, open_selection

            choices = ctx.providers.model_preferences.snapshot()["sources"][provider_id]
            if not choices["enabled"] or not choices["models"]:
                raise ValueError("Selected model is disabled")
            selection = capture_selection(provider_id, {"model": choices["models"][0]})
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
    selected_provider: Any,
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
    ctx: TaskState,
    *,
    parent_policy: Any,
    project: str | Path,
    selected_provider: Any = None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    with ExitStack() as leases:
        return run_project_audit_core(
            parent_policy=parent_policy,
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


def run_research_advisors(
    ctx: TaskState,
    *,
    selected_provider: Any,
    selected_provider_id: str,
    pack: EvidencePack,
) -> tuple[ConsensusAdvice, ...]:
    from codey.research.advisors import run_research_advisors as run_research_advisors_core

    with ExitStack() as leases:
        return run_research_advisors_core(
            selected_provider_id=selected_provider_id,
            provider_ids=tuple(PROVIDER_LABELS),
            provider_labels=PROVIDER_LABELS,
            availability=lambda: providers.provider_availability(ctx),
            connect_existing=lambda provider_id: _scoped_advisor(ctx, selected_provider, provider_id, leases),
            clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
            pack=pack,
        )


__all__ = [
    "connect_consensus_provider",
    "run_consensus",
    "run_project_audit",
    "run_research_advisors",
]
