from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.connector_search import ConnectorAwareSearchProvider, _read_url_text
from codey.research.source_connectors import SourceConnectorRegistry, SourceConnectorSpec
from codey.research.tools import ResearchTools


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "research_connectors"


class FakeBaseSearchProvider:
    name = "fake_browser"

    def __init__(self) -> None:
        self.searches: list[tuple[str, int]] = []
        self.fetches: list[str] = []

    def search(self, query: str, limit: int = 8) -> list[dict]:
        self.searches.append((query, limit))
        return [{
            "title": "Generic web result",
            "url": "https://example.com/generic",
            "snippet": "generic snippet",
        }]

    def fetch(self, url: str) -> dict:
        self.fetches.append(url)
        return {
            "url": url,
            "title": "Generic",
            "text": "generic page body",
            "truncated": False,
        }


def _fixture_response(url: str, *, timeout: float) -> str:
    del timeout
    if "esearch.fcgi" in url:
        return json.dumps({"esearchresult": {"idlist": ["12345678"]}})
    if "efetch.fcgi" in url:
        return (FIXTURES / "pubmed.xml").read_text(encoding="utf-8")
    if "export.arxiv.org" in url:
        return (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")
    raise AssertionError(url)


def test_connector_aware_search_adds_pubmed_result_and_open_url_reads_connector_document() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=_fixture_response):
        results = provider.search("clinical hepatotoxicity patient therapy", limit=3)

    assert results[0]["title"].startswith("PubMed:")
    assert results[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert results[1]["url"] == "https://example.com/generic"
    assert base.searches == [("clinical hepatotoxicity patient therapy", 3)]

    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td))
        tools = ResearchTools(provider, store, KnowledgeChanges(store.root))
        opened = tools.open_url(results[0]["url"])
        store.close()

    assert "hepatotoxicity" in opened
    assert results[0]["url"] in tools.sources_read
    assert base.fetches == []
    assert tools.ledger.opened_sources_payload()[0]["final_url"] == results[0]["url"]


def test_connector_live_search_uses_shared_medical_routing_terms() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)
    requested: list[str] = []

    def response(url: str, *, timeout: float) -> str:
        del timeout
        requested.append(url)
        return _fixture_response(url, timeout=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=response):
        results = provider.search("genetic association study", limit=3)

    assert results[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert any("eutils.ncbi.nlm.nih.gov" in item for item in requested)


def test_connector_live_query_strips_secret_url_and_path_before_api_request() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)
    requested: list[str] = []

    def response(url: str, *, timeout: float) -> str:
        del timeout
        assert base.searches == [(
            "clinical hepatotoxicity SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX "
            "https://example.com/items?token=SECRET E:/secret/project",
            3,
        )]
        requested.append(url)
        return _fixture_response(url, timeout=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=response):
        provider.search(
            "clinical hepatotoxicity SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX "
            "https://example.com/items?token=SECRET E:/secret/project",
            limit=3,
        )

    joined = "\n".join(requested)
    assert "clinical" in joined
    assert "hepatotoxicity" in joined
    assert "SECRET" not in joined
    assert "example.com" not in joined
    assert "E%3A" not in joined
    assert "/secret/project" not in joined
    assert "tool=research_connector" in joined
    assert "tool=codey" not in joined


def test_connector_live_query_strips_natural_language_secret_values_before_api_request() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)
    requested: list[str] = []

    def response(url: str, *, timeout: float) -> str:
        del timeout
        requested.append(url)
        return _fixture_response(url, timeout=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=response):
        provider.search("password is equal to hunter2 clinical cancer", limit=3)
        provider.search("api key named abcdef clinical cancer", limit=3)
        provider.search("api key called livekey clinical cancer", limit=3)
        provider.search("client secret known as abcdef clinical cancer", limit=3)
        provider.search("password is configured as hunter2 clinical cancer", limit=3)
        provider.search("密码 是 hunter2 临床 癌症", limit=3)
        provider.search("密钥等于 abcdef clinical cancer", limit=3)

    joined = "\n".join(requested)
    assert "clinical" in joined
    assert "cancer" in joined
    assert "livekey" not in joined
    assert "hunter2" not in joined
    assert "abcdef" not in joined
    assert "password" not in joined
    assert "api" not in joined
    assert "key" not in joined
    assert "client" not in joined
    assert "secret" not in joined


def test_connector_live_search_honors_registry_unavailable_status() -> None:
    base = FakeBaseSearchProvider()
    registry = SourceConnectorRegistry((
        SourceConnectorSpec(
            id="pubmed",
            kind="biomedical_literature",
            status="unavailable",
            search_supported=True,
            fetch_supported=True,
            fixture_supported=False,
            shipped=False,
        ),
        SourceConnectorSpec(
            id="arxiv",
            kind="academic_preprint",
            status="available",
            search_supported=True,
            fetch_supported=True,
            fixture_supported=False,
            shipped=True,
        ),
    ))
    provider = ConnectorAwareSearchProvider(base, registry=registry, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text") as read:
        results = provider.search("clinical genetic cancer therapy", limit=2)

    assert results == [{
        "title": "Generic web result",
        "url": "https://example.com/generic",
        "snippet": "generic snippet",
    }]
    read.assert_not_called()


def test_connector_request_timeout_never_rounds_past_remaining_budget() -> None:
    provider = ConnectorAwareSearchProvider(
        FakeBaseSearchProvider(),
        rate_limit=False,
        connector_budget_seconds=1.0,
    )
    provider._search_deadline = 10.25

    with mock.patch("codey.research.connector_search.time.monotonic", return_value=10.0):
        assert provider._request_timeout() == pytest.approx(0.25)

    with mock.patch("codey.research.connector_search.time.monotonic", return_value=10.06):
        with pytest.raises(TimeoutError, match="budget exhausted"):
            provider._request_timeout()


def test_connector_http_user_agent_does_not_name_product() -> None:
    response = mock.MagicMock()
    response.read.return_value = b"{}"
    response.headers.get_content_charset.return_value = "utf-8"
    response.__enter__.return_value = response

    with mock.patch("codey.research.connector_search.urllib.request.Request") as request:
        with mock.patch("codey.research.connector_search.urllib.request.urlopen", return_value=response):
            assert _read_url_text("https://export.arxiv.org/api/query", timeout=1) == "{}"

    headers = request.call_args.kwargs["headers"]
    assert headers["User-Agent"] == "Research Connector"
    assert "Codey" not in headers["User-Agent"]


def test_connector_search_skips_api_when_safe_query_is_empty() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text") as read:
        results = provider.search(
            "pubmed SECRET_TOKEN_ABCDEFGHIJKLMNOPQRSTUVWX "
            "https://example.com/items?token=SECRET E:/secret/project",
            limit=2,
        )

    assert results == [{
        "title": "Generic web result",
        "url": "https://example.com/generic",
        "snippet": "generic snippet",
    }]
    read.assert_not_called()


def test_connector_aware_search_adds_arxiv_result_and_deduplicates_base_result() -> None:
    base = FakeBaseSearchProvider()

    def base_search(query: str, limit: int = 8) -> list[dict]:
        base.searches.append((query, limit))
        return [
            {
                "title": "Duplicate arXiv",
                "url": "https://arxiv.org/abs/2401.01234v1",
                "snippet": "duplicate",
            },
            {
                "title": "Other",
                "url": "https://example.com/other",
                "snippet": "other",
            },
        ]

    base.search = base_search  # type: ignore[method-assign]
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=_fixture_response):
        results = provider.search("arxiv transformer diffusion model evaluation", limit=4)

    urls = [item["url"] for item in results]
    assert urls == [
        "https://arxiv.org/abs/2401.01234v1",
        "https://example.com/other",
    ]


def test_connector_merge_preserves_distinct_base_urls_that_differ_by_query() -> None:
    base = FakeBaseSearchProvider()

    def base_search(query: str, limit: int = 8) -> list[dict]:
        base.searches.append((query, limit))
        return [
            {
                "title": "Item 1",
                "url": "https://example.com/item?id=1",
                "snippet": "one",
            },
            {
                "title": "Item 2",
                "url": "https://example.com/item?id=2",
                "snippet": "two",
            },
        ]

    base.search = base_search  # type: ignore[method-assign]
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    results = provider.search("ordinary web lookup", limit=4)

    assert [item["url"] for item in results] == [
        "https://example.com/item?id=1",
        "https://example.com/item?id=2",
    ]


def test_connector_aware_arxiv_generation_query_does_not_trigger_pubmed() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)
    calls: list[str] = []

    def response(url: str, *, timeout: int) -> str:
        del timeout
        calls.append(url)
        if "eutils.ncbi.nlm.nih.gov" in url:
            raise AssertionError("generation must not trigger PubMed via gene substring")
        return (FIXTURES / "arxiv_atom.xml").read_text(encoding="utf-8")

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=response):
        results = provider.search("retrieval augmented generation evaluation benchmark", limit=3)

    assert results[0]["url"] == "https://arxiv.org/abs/2401.01234v1"
    assert all("eutils.ncbi.nlm.nih.gov" not in item for item in calls)


def test_pubmed_live_search_filters_invalid_ids_before_fetch() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=3)
    requested: list[str] = []

    def response(url: str, *, timeout: float) -> str:
        del timeout
        requested.append(url)
        if "esearch.fcgi" in url:
            return json.dumps({
                "esearchresult": {
                    "idlist": ["12345678", "1234567890123", "SECRET_TOKEN"]
                }
            })
        if "efetch.fcgi" in url:
            return (FIXTURES / "pubmed.xml").read_text(encoding="utf-8")
        raise AssertionError(url)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=response):
        provider.search("clinical hepatotoxicity patient therapy", limit=4)

    fetch_urls = [item for item in requested if "efetch.fcgi" in item]
    assert len(fetch_urls) == 1
    assert "id=12345678" in fetch_urls[0]
    assert "1234567890123" not in fetch_urls[0]
    assert "SECRET_TOKEN" not in fetch_urls[0]


def test_connector_aware_search_falls_back_to_browser_when_connector_fails() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=ValueError("network down")):
        results = provider.search("clinical cancer therapy", limit=2)

    assert results == [{
        "title": "Generic web result",
        "url": "https://example.com/generic",
        "snippet": "generic snippet",
    }]
    assert provider.last_connector_errors[-1]["connector_id"] == "pubmed"


def test_connector_aware_fetch_direct_pubmed_url_uses_connector_before_browser() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=_fixture_response):
        page = provider.fetch("https://pubmed.ncbi.nlm.nih.gov/12345678/")

    assert page["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert "hepatotoxicity" in page["text"]
    assert base.fetches == []


def test_connector_aware_fetch_direct_pubmed_url_falls_back_to_browser_when_connector_lookup_fails() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=ValueError("network down")):
        page = provider.fetch("https://pubmed.ncbi.nlm.nih.gov/12345678/")

    assert page["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert page["text"] == "generic page body"
    assert base.fetches == ["https://pubmed.ncbi.nlm.nih.gov/12345678/"]
    assert provider.last_connector_errors[-1]["action"] == "fetch_lookup"


def test_connector_aware_fetch_direct_arxiv_url_falls_back_to_browser_when_connector_lookup_fails() -> None:
    base = FakeBaseSearchProvider()
    provider = ConnectorAwareSearchProvider(base, rate_limit=False, connector_limit=1)

    with mock.patch("codey.research.connector_search._read_url_text", side_effect=ValueError("network down")):
        page = provider.fetch("https://arxiv.org/abs/2401.01234")

    assert page["url"] == "https://arxiv.org/abs/2401.01234"
    assert page["text"] == "generic page body"
    assert base.fetches == ["https://arxiv.org/abs/2401.01234"]
    assert provider.last_connector_errors[-1]["connector_id"] == "arxiv"
    assert provider.last_connector_errors[-1]["action"] == "fetch_lookup"
