"""Search backend failure must remain distinguishable from no matching hits."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from codey.research.connector_search import ConnectorAwareSearchProvider
from codey.research.tools import ResearchTools
from codey.runtime.core.cancellation import DeadlineExceeded, TaskCancelled


def tools_for(base):
    return ResearchTools(search=ConnectorAwareSearchProvider(base, connector_ids=()), store=None, changes=None)


def test_browser_timeout_is_an_error_tool_result_without_search_fact():
    base = SimpleNamespace(search=mock.Mock(side_effect=TimeoutError()))
    tools = tools_for(base)
    result = tools.web_search("pricing discount context")
    assert result.startswith("ERROR:"), result
    assert "TimeoutError" in result
    assert not tools.search_result_urls
    assert tools.ledger.searches == []


def test_actual_empty_response_is_still_a_valid_no_results():
    tools = tools_for(SimpleNamespace(search=lambda *a, **kw: []))
    assert tools.web_search("no matching document") == "no results"


def test_browser_failure_preserves_real_partial_connector_hits():
    provider = tools_for(SimpleNamespace(search=mock.Mock(side_effect=TimeoutError()))).search
    hits = [{"url": "https://example.org/article", "title": "Real connector hit", "snippet": "Observed result."}]
    with mock.patch.object(provider, "_connector_search_results", return_value=hits):
        result = provider.search("pricing discount")
    assert [hit["url"] for hit in result] == ["https://example.org/article"]


@pytest.mark.parametrize("error", [TaskCancelled("stopped"), DeadlineExceeded("deadline expired")])
def test_partial_hits_do_not_swallow_cancellation(error):
    provider = tools_for(SimpleNamespace(search=mock.Mock(side_effect=error))).search
    with (
        mock.patch.object(provider, "_connector_search_results", return_value=[{"url": "https://example.org/article"}]),
        pytest.raises(type(error)),
    ):
        provider.search("pricing discount")


@pytest.mark.parametrize("error", [TaskCancelled("stopped"), DeadlineExceeded("deadline expired")])
def test_search_tool_propagates_cancellation_instead_of_rendering_error(error):
    tools = tools_for(SimpleNamespace(search=mock.Mock(side_effect=error)))
    with pytest.raises(type(error)):
        tools.web_search("pricing discount")
