"""Acquisition cancellation is never a search empty result or rendering fallback."""
from __future__ import annotations

import pytest

from codey.automation.browser_contracts import BrowserToolOutput
from codey.automation.browser_session import BrowserSession
from codey.research.connector_search import ScholarlySources
from codey.research.ledger import ResearchLedger
from codey.research.source_connectors import SourceHit
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.task_browser_source import TaskBrowserSource
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled
from tests.test_search_failure_is_not_empty_success import Source


@pytest.mark.parametrize("error", [TaskCancelled, DeadlineExceeded])
def test_gateway_preserves_task_stop_without_source_evidence(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    class StoppedSource(Source):
        def fetch(self, url: str) -> dict[str, object]:
            raise error("stop acquisition")
    def allow(url: str, *, use_cache: bool) -> None:
        return None
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", allow)
    ledger = ResearchLedger()
    with pytest.raises(error):
        ResearchSourceGateway(StoppedSource(), ledger).open("https://example.com/article")
    assert not ledger.opened_sources and not ledger.read_passages


@pytest.mark.parametrize("error", [TaskCancelled, DeadlineExceeded])
def test_rendering_navigation_preserves_deadline_exception(error: type[Exception]) -> None:
    class StoppedBrowser(BrowserSession):
        def navigate(self, url: str) -> BrowserToolOutput:
            raise error("stop navigation")
    with pytest.raises(error):
        TaskBrowserSource(StoppedBrowser()).fetch("https://example.com/article")


@pytest.mark.parametrize("error", [TaskCancelled, DeadlineExceeded])
def test_connector_lookup_preserves_task_stop(error: type[Exception]) -> None:
    class StoppedConnector(ScholarlySources):
        def _fetchable_hit_for_url(self, url: str) -> SourceHit | None:
            raise error("stop lookup")
    with pytest.raises(error):
        StoppedConnector().fetch("https://example.com/article")
