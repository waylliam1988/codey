"""Revalidate the evaluated project history treatment without model requests."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit

from codey.agents.request import AgentRequest
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observation_index import (
    MAX_RETRIEVED_ITEMS,
    RETRIEVAL_BUDGET_CHARS,
    render_retrieved_block,
    retrieve_relevant_observations,
)
from codey.ghost.observations import GhostObservationStore
from codey.providers.compat_transport import provider_attribute
from codey.providers.token_accounting import ContextBudget

VERIFIED_PROJECT_EXPERIENCE_MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"


def _configuration_enabled(provider: object) -> bool:
    # This treatment has its own promotion boundary: widening the separately
    # evaluated language directive must not enable project history elsewhere.
    if provider_attribute(provider, "model") != VERIFIED_PROJECT_EXPERIENCE_MODEL:
        return False
    endpoint = urlsplit(str(provider_attribute(provider, "base_url") or ""))
    budget = provider_attribute(provider, "context_budget")
    return (
        endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
        and endpoint.path.rstrip("/") == "/v1"
        and provider_attribute(provider, "api_protocol") == "openai-completions"
        and provider_attribute(provider, "native_tools") is True
        and provider_attribute(provider, "temperature") == 0
        and provider_attribute(provider, "stream") is False
        and isinstance(budget, ContextBudget)
        and budget.window_tokens == 262144
        and budget.output_tokens == 128 and budget.safety_tokens == 32640
        and budget.keep_recent_tokens == 32000 and budget.input_limit_tokens is None
    )


def validated_project_experiences(request: AgentRequest) -> str:
    """Return only current, in-scope history for the frozen model/config.

    Re-reading committed rows checks deletion, consumption windows and durable
    input authority. Lexical ranking never calls an embedding service.
    """
    if not request.admit_hints or not request.ghost_experiences:
        return ""
    if request.permission_profile != "coding_writer" or request.workspace_revision_store is None:
        return ""
    try:
        if not _configuration_enabled(request.provider):
            return ""
        home = request.workspace_revision_store.state_home
        inbox = GhostInboxStore(home)
        for control in ("retrieve_projections", "admit_hints"):
            if not inbox.control_enabled(control, project=str(request.project), session_id=request.session_id):
                return ""
        store = GhostObservationStore(home)
        rows = tuple(row for row in store.read_committed(
            project=str(request.project), session_id=request.session_id, scope="project",
        ) if row["run_id"] != request.run_id)
        picked = retrieve_relevant_observations(
            rows, request.task, max_items=MAX_RETRIEVED_ITEMS, budget_chars=RETRIEVAL_BUDGET_CHARS,
        )
        if not picked:
            return ""
        current = render_retrieved_block(picked, RETRIEVAL_BUDGET_CHARS)
        return current if current == request.ghost_experiences else ""
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ""


__all__ = ["validated_project_experiences"]
