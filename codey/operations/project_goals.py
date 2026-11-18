"""The evaluated confirmed storage-goal treatment for the normal writer."""

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit

from codey.agents.request import AgentRequest
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.project_goals import GOAL_BUDGET, HISTORY_BUDGET, confirmed_project_goal_text
from codey.providers.compat_transport import provider_attribute
from codey.providers.token_accounting import ContextBudget
from codey.workspace.context_admission import GhostContextSource, request_scope
from codey.workspace.context_source import ContextSource, render_context_source

VERIFIED_PROJECT_GOAL_MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"


def _configuration_enabled(provider: object) -> bool:
    endpoint = urlsplit(str(provider_attribute(provider, "base_url") or ""))
    budget = provider_attribute(provider, "context_budget")
    return (
        provider_attribute(provider, "model") == VERIFIED_PROJECT_GOAL_MODEL
        and endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
        and endpoint.path.rstrip("/") == "/v1"
        and provider_attribute(provider, "api_protocol") == "openai-completions"
        and provider_attribute(provider, "native_tools") is True
        and provider_attribute(provider, "temperature") == 0
        and provider_attribute(provider, "stream") is False
        and isinstance(budget, ContextBudget)
        and budget.window_tokens == 262144 and budget.output_tokens == 8192 and budget.safety_tokens == 24576
        and budget.keep_recent_tokens == 32000 and budget.input_limit_tokens is None
    )


def validated_project_goal_sources(request: AgentRequest) -> tuple[GhostContextSource, ...]:
    from codey.policies.permissions import allows_context_source, profile_for_name

    if (not request.admit_hints or request.permission_profile != "coding_writer"
            or request.workspace_revision_store is None):
        return ()
    # This consumer concerns storage tradeoffs, rather than every project conversation.
    if not any(term in request.task.casefold() for term in ("storage", "存储", "儲存")):
        return ()
    profile = profile_for_name(request.permission_profile)
    if not all(allows_context_source(profile, key) for key in
               ("ghost_project_goal_history", "ghost_confirmed_project_goal")):
        return ()
    try:
        if not _configuration_enabled(request.provider):
            return ()
        home = request.workspace_revision_store.state_home
        inbox = GhostInboxStore(home)
        observations = GhostObservationStore(home)
        with inbox.database.write_transaction():
            if not all(inbox.control_enabled(control, project=str(request.project), session_id=request.session_id)
                       for control in ("retrieve_projections", "admit_hints")):
                return ()
            history, goal = confirmed_project_goal_text(inbox, observations, project=str(request.project),
                                                       exclude_run_id=request.run_id)
        if not history or not goal:
            return ()
        scope = request_scope(request.project, request.session_id)
        sources: list[GhostContextSource] = []
        for key, value, budget in (("ghost_project_goal_history", history, HISTORY_BUDGET),
                                   ("ghost_confirmed_project_goal", goal, GOAL_BUDGET)):
            def load_value(text: str = value) -> str:
                return text

            source = render_context_source(ContextSource(key, load_value, budget, "committed_goal_snapshot",
                                                         "manual_project_goal", scope=scope))
            if source is None or source.truncated:
                return ()
            sources.append(GhostContextSource(key, source.text, budget, scope))
        return tuple(sources)
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        return ()
