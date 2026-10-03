"""Acquisition cancellation/deadlines must not trigger fallback I/O or evidence."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from codey.research.browser_search import BrowserSearchProvider
from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled


def test_gateway_open_preserves_deadline_exception_without_evidence():
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=Mock(side_effect=DeadlineExceeded("budget"))), ledger)
    with patch("codey.research.source_gateway.check_fetch_url", return_value=""), pytest.raises(DeadlineExceeded):
        gateway.open("https://example.com/guide")
    assert ledger.final_url_set() == set()


def test_browser_deadline_is_not_downgraded_to_navigation_timeout():
    provider = BrowserSearchProvider()
    with patch("codey.research.browser_search._search_browser_call", side_effect=DeadlineExceeded("budget")), patch.object(provider, "_record_worker_health"), pytest.raises(DeadlineExceeded):
        provider.fetch("https://example.com/guide")


@pytest.mark.parametrize("error", [TaskCancelled, DeadlineExceeded])
@pytest.mark.parametrize("stage", ["lookup", "fetch"])
def test_connector_lifecycle_exception_never_calls_browser_fallback(stage, error):
    base = SimpleNamespace(fetch=Mock(side_effect=AssertionError("must not fall back")))
    provider = ConnectorAwareSearchProvider(base, rate_limit=False)
    hit = SimpleNamespace(connector_id="arxiv")
    lookup = Mock(side_effect=error("stop")) if stage == "lookup" else Mock(return_value=hit)
    with patch.object(provider, "_fetchable_hit_for_url", lookup), patch("codey.research.connector_search.fetch_recorded_hit", side_effect=error("stop")), pytest.raises(error):
        provider.fetch("https://example.com/guide")
    base.fetch.assert_not_called()
