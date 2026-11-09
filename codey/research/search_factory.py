"""Single owner for the default research search provider (leaf).

Execution resources, the research flow, and dispatch all consume this
factory. The leaf owns provider construction and imports no operations
modules, so execution no longer pulls the research flow back into the
unified kernel.
"""

from __future__ import annotations

from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider


def default_research_search_provider() -> ConnectorAwareSearchProvider:
    return ConnectorAwareSearchProvider(BrowserSearchProvider(isolated=False))


__all__ = ["default_research_search_provider"]
