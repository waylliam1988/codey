"""Scholarly execution, query privacy and composition with the one search coordinator."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codey.research import connector_search
from codey.research.connector_search import ScholarlySources
from codey.research.search_contracts import SearchState
from codey.research.source_connectors import safe_connector_query
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core import cancellation
from tests.test_chat_web_provider import Browser

FIXTURES = Path(__file__).parent / "fixtures" / "research_connectors"


def fixture_response(url: str, *, timeout: float) -> str:
    if "esearch.fcgi" in url:
        return json.dumps({"esearchresult": {"idlist": ["invalid", "12345678"]}})
    if "efetch.fcgi" in url:
        assert "invalid" not in url
        return (FIXTURES / "pubmed.xml").read_text(encoding="utf-8")
    if "export.arxiv.org" in url:
        return (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")
    raise AssertionError(url)


@pytest.mark.parametrize("query,engine", [
    ("clinical hepatotoxicity patient therapy", "pubmed"),
    ("quantum astronomy arxiv preprints", "arxiv"),
])
def test_connector_results_and_actual_query_keep_coverage(monkeypatch: pytest.MonkeyPatch, query: str, engine: str) -> None:
    monkeypatch.setattr(connector_search, "_read_url_text", fixture_response)
    source = ScholarlySources(rate_limit=False, connector_limit=1)
    outcome = source.search(query, limit=3, deadline=time.monotonic() + 5)
    assert outcome is not None and outcome.state == SearchState.HITS
    assert outcome.attempts[0].engine == engine
    assert outcome.attempts[0].effective_query
    assert all(attempt.coverage == "scholarly" for attempt in outcome.attempts)
    assert outcome.hits[0].url.startswith("https://" + ("pubmed.ncbi.nlm.nih.gov" if engine == "pubmed" else "arxiv.org"))


def test_general_query_does_not_create_spurious_scholarly_failure() -> None:
    source = ScholarlySources()
    assert source.search("Python pathlib official docs", limit=3, deadline=time.monotonic() + 5) is None


def test_connector_privacy_corpus_never_enters_api_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    def response(url: str, *, timeout: float) -> str:
        requested.append(url)
        return fixture_response(url, timeout=timeout)
    monkeypatch.setattr(connector_search, "_read_url_text", response)
    source = ScholarlySources(rate_limit=False, connector_limit=1)
    queries = (
        "api - key - abcdef clinical cancer",
        "api key one two three clinical cancer",
        "api:key abcdef clinical cancer",
        "private - key topsecret clinical cancer",
        "private key one two three clinical cancer",
        "access + key abcdef clinical cancer",
        "password . is . called . livekey clinical cancer",
        "client · secret abcdef clinical cancer",
        "client secret one two three clinical cancer",
        "password hunter2 clinical cancer",
        "access_token abcdef clinical cancer",
        "access_token one two three clinical cancer",
        "token abcdef clinical cancer",
        "id_token abcdef clinical cancer",
        "id_token one two three clinical cancer",
        "auth_token abcdef clinical cancer",
        "auth_token one two three clinical cancer",
        "api_token abcdef clinical cancer",
        "api_token one two three clinical cancer",
        "cookie abcdef clinical cancer",
        "jwt abcdef clinical cancer",
        "bearer_token abcdef clinical cancer",
        "bearer_token one two three clinical cancer",
        "password correct horse battery staple clinical cancer",
        "passphrase correct horse battery staple clinical cancer",
        "密 - 钥 abcdef 临床 癌症",
        "密钥 abcdef 临床 癌症",
        "密钥 one two three 临床 癌症",
        "密码 hunter2 临床 癌症",
    )
    for query in queries:
        outcome = source.search(query, limit=3, deadline=time.monotonic() + 5)
        assert outcome is not None and outcome.hits
    joined = "\n".join(requested).casefold()
    for forbidden in ("abcdef", "topsecret", "livekey", "hunter2", "correct", "horse", "battery", "staple"):
        assert forbidden not in joined


def test_one_coordinator_keeps_real_connector_hits_when_web_search_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.research.search_contracts import SearchOutcome, failed_search
    from codey.research.search_profiles import SearchProfile
    class FailedBrowser(Browser):
        def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome:
            return failed_search(query, profile.name, "search_page_blank")
    monkeypatch.setattr(connector_search, "_read_url_text", fixture_response)
    provider = WebSourceProvider(browser_factory=FailedBrowser, scholarly=ScholarlySources(rate_limit=False))
    outcome = provider.search("clinical hepatotoxicity")
    assert outcome.state == SearchState.HITS
    assert outcome.hits[0].url.startswith("https://pubmed.ncbi.nlm.nih.gov/")
    assert outcome.errors[0]["failure_kind"] == "search_page_blank"


@pytest.mark.parametrize("error", [cancellation.TaskCancelled, cancellation.DeadlineExceeded])
def test_connector_stop_is_not_translated_into_endpoint_failure(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    def stopped(url: str, *, timeout: float) -> str:
        raise error("stop")
    monkeypatch.setattr(connector_search, "_read_url_text", stopped)
    source = ScholarlySources(rate_limit=False)
    with pytest.raises(error):
        source.search("clinical cancer", limit=3, deadline=time.monotonic() + 5)
    assert source._search_deadline is None


def test_single_safe_query_preserves_contextual_domain_terms() -> None:
    query = safe_connector_query("clinical token expression receptor antibody", limit=12)
    assert "clinical" in query.terms
