"""Own construction and cleanup of task-scoped Research and browser resources."""
from __future__ import annotations

import contextlib

from codey.knowledge.store import KnowledgeStore
from codey.research.source_gateway import _SearchProvider
from codey.research.tools import ResearchTools


def close_research_tools(tools: ResearchTools | None) -> None:
    """Release an owned search session without masking the task outcome."""
    search = tools.search if tools is not None else None
    close: object = getattr(search, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


def bind_browser_session(resources: ResearchTools) -> None:
    """Attach the shared page resource once, for every production kernel entry."""
    if resources.browser_session is None:
        from codey.operations.browser.browser_resources import bind_source_browser, source_browser

        resources.browser_session = source_browser(resources)
        bind_source_browser(resources, resources.browser_session)


def build_research_tools(deps: object, *, session_id: str, project: str, browser_sources: bool = True,
                         browser_actions: bool = False) -> ResearchTools:
    """Build the execution resources; configuration errors propagate to entry."""
    from codey.knowledge.changes import KnowledgeChanges
    from codey.research.tools import ResearchTools

    knowledge_store: object = getattr(deps, "knowledge_store", None)
    if knowledge_store is not None and not isinstance(knowledge_store, KnowledgeStore):
        raise TypeError("invalid knowledge store")
    search_factory: object = getattr(deps, "search_factory", None)
    if search_factory is None:
        from codey.research.search_factory import default_research_search_provider
        from codey.research.web_sources import WebSourceProvider

        search_factory = default_research_search_provider if browser_sources or browser_actions else WebSourceProvider
    if not callable(search_factory):
        raise TypeError("invalid search factory")
    search: object = search_factory()
    if not isinstance(search, _SearchProvider):
        raise TypeError("invalid search provider")
    try:
        resources = ResearchTools(
            search=search, store=knowledge_store,
            changes=KnowledgeChanges(root=knowledge_store.root) if knowledge_store is not None else None,
            diagnostics=None, session_id=session_id, project=project,
        )
        if browser_actions:
            bind_browser_session(resources)
        return resources
    except BaseException:
        close: object = getattr(search, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
        raise
