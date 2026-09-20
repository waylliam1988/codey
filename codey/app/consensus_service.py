"""Second-model consensus, audit, and research-advisor runs.

``task_submit`` builds ``TaskRunDeps.run_consensus`` / ``run_project_audit`` /
``run_research_advisors`` from these. Import-light by design (see
``test_server_lazy_state``): the research core stays function-local, nothing
here may load the browser stack at import time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codey.research.advisors import EvidencePack

from codey.agents.consensus import (
    ConsensusAdvice,
    ConsensusResult,
)
from codey.agents.consensus import (
    run_consensus as run_consensus_core,
)
from codey.agents.consensus import (
    run_project_audit as run_project_audit_core,
)
from codey.app import provider_services as providers
from codey.operations.task_state import TaskState
from codey.providers.catalog import PROVIDER_LABELS


def connect_consensus_provider(selected_provider, provider_id: str):
    """Use an already-open sibling tab while a Writer provider is active."""

    if provider_id == "local":
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


def run_consensus(
    ctx: TaskState,
    *,
    selected_provider,
    selected_provider_id: str,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    trace_recorder: object | None = None,
) -> ConsensusResult | None:
    return run_consensus_core(
        selected_provider=selected_provider,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: providers.provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
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
    project: str | Path,
    selected_provider=None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    return run_project_audit_core(
        project=project,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: providers.provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        context=context,
        trace_recorder=trace_recorder,
    )


def run_research_advisors(
    ctx: TaskState,
    *,
    selected_provider,
    selected_provider_id: str,
    pack: EvidencePack,
) -> tuple[ConsensusAdvice, ...]:
    from codey.research.advisors import run_research_advisors as run_research_advisors_core

    return run_research_advisors_core(
        selected_provider_id=selected_provider_id,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: providers.provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        pack=pack,
    )


__all__ = [
    "connect_consensus_provider",
    "run_consensus",
    "run_project_audit",
    "run_research_advisors",
]
