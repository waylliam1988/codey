from __future__ import annotations

from unittest.mock import Mock

import pytest

from codey.runtime.core import cancellation
from tests.manual import research_live_web_local_ab as experiment


def provider(cls, rows, error=None):
    base = Mock()
    base.search.side_effect = error
    base.search.return_value = [{"title": "Official guidance", "url": "https://official.example/guidance"}]
    result = cls(base, connector_ids=())
    result._connector_search_results = Mock(return_value=rows)
    return result, base


HITS = [{"title": f"Paper {n}", "url": f"https://pubmed.ncbi.nlm.nih.gov/{n}/", "snippet": ""} for n in (1, 2)]


def test_candidate_only_removes_early_stop_and_keeps_connector_hits():
    baseline, baseline_browser = provider(experiment.ObservedProduction, HITS)
    candidate, candidate_browser = provider(experiment.BrowserAfterConnector, HITS)
    assert baseline.search("PubMed evidence") == HITS
    baseline_browser.search.assert_not_called()
    rows = candidate.search("PubMed evidence")
    assert rows[:2] == HITS
    assert rows[2]["url"] == "https://official.example/guidance"
    candidate_browser.search.assert_called_once()


def test_browser_failure_preserves_available_connector_evidence():
    candidate, _ = provider(experiment.BrowserAfterConnector, HITS, RuntimeError("browser unavailable"))
    assert candidate.search("PubMed evidence") == HITS


def test_failure_without_connector_hits_is_not_hidden():
    candidate, _ = provider(experiment.BrowserAfterConnector, [], RuntimeError("browser unavailable"))
    with pytest.raises(RuntimeError, match="browser unavailable"):
        candidate.search("PubMed evidence")


def test_candidate_preserves_cancellation():
    candidate, _ = provider(experiment.BrowserAfterConnector, HITS, cancellation.TaskCancelled())
    with pytest.raises(cancellation.TaskCancelled):
        candidate.search("PubMed evidence")
