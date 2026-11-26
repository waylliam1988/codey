"""Chat source discovery uses the same typed coordinator and lazy browser."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.operations.task_execution import build_research_tools
from codey.research.search_contracts import SearchOutcome, complete_search
from codey.research.search_profiles import SearchProfile
from codey.research.search_provenance import SearchAttempt
from codey.research.source_acquisition import attempt, with_attempt
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject


class Browser:
    def __init__(self) -> None:
        self.searches: list[tuple[str, int]] = []
        self.fetches: list[str] = []
        self.closed = 0
        self.error: Exception | None = None
        self.hits: list[JsonObject] = [{"title": "Food", "url": "https://example.com/food?a=1&b=2", "snippet": "Observed"}]

    def search(self, query: str, *, limit: int, profile: SearchProfile, deadline: float) -> SearchOutcome:
        self.searches.append((query, limit))
        if self.error is not None:
            raise self.error
        return complete_search(query, self.hits, SearchAttempt(query, profile.name), limit, explicit_empty=not self.hits)

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches.append(url)
        return {"status": "ok", "url": url, "title": "Rendered", "text": "Rendered page body"}

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("query", ["福州 美食 肉燕+鱼丸 & 地方小吃", "东百中心附近有什么好玩的", "东百中心有什么好玩的"])
def test_lazy_search_preserves_original_query(query: str) -> None:
    browser = Browser()
    created: list[bool] = []
    def create() -> Browser:
        created.append(True)
        return browser
    provider = WebSourceProvider(browser_factory=create)
    assert not created
    assert provider.search(query, limit=3).rows == browser.hits
    assert browser.searches == [(query, 3)]
    provider.close()
    assert browser.closed == 1


@pytest.mark.parametrize("error", [cancellation.TaskCancelled("stop"), cancellation.DeadlineExceeded("stop")])
def test_search_interruption_propagates_without_next_engine(error: Exception) -> None:
    browser = Browser()
    browser.error = error
    provider = WebSourceProvider(browser_factory=lambda: browser)
    with pytest.raises(type(error)):
        provider.search("query")
    assert len(browser.searches) == 1
    provider.close()


def test_search_and_render_share_browser_but_static_reads_do_not_start_it(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = Browser()
    provider = WebSourceProvider(browser_factory=lambda: browser)
    def http(url: str) -> dict[str, object]:
        return with_attempt({"status": "ok", "text": "Static"}, "http", url)
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http)
    assert provider.fetch("https://example.com/static")["text"] == "Static"
    assert provider._browser is None
    browser.hits = []
    assert provider.search("empty search").rows == []
    def pending(url: str) -> dict[str, object]:
        return with_attempt({"status": "error", "failure_code": "page_not_loaded"}, "http", url)
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", pending)
    assert provider.fetch("https://example.com/spa")["text"] == "Rendered page body"
    provider.close()
    provider.close()
    assert browser.closed == 1


def test_chat_resources_select_chat_provider_lazily_and_keep_injected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def research() -> None:
        pytest.fail("ordinary chat constructed the strict Research provider")

    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", research)
    resources = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=None),
                                     session_id="s", project="", browser_sources=False)
    assert isinstance(resources.search, WebSourceProvider)
    injected = WebSourceProvider()
    resources = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=lambda: injected),
                                     session_id="s", project="", browser_sources=False)
    assert resources.search is injected


@pytest.mark.parametrize("browser_sources", [False, True])
def test_explicit_factory_is_respected_for_both_chat_and_research(
    monkeypatch: pytest.MonkeyPatch, browser_sources: bool,
) -> None:
    from codey.research.ledger import ResearchLedger
    from codey.research.source_gateway import ResearchSourceGateway

    selected = Browser()
    tools = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=lambda: selected),
        session_id="s", project="", browser_sources=browser_sources)
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **k: None)
    url = "https://example.com/"
    assert tools.search is not None
    result = ResearchSourceGateway(tools.search, ResearchLedger(), direct_urls=frozenset({url})).open(url)
    assert result.status == "ok" and result.document is not None
    assert result.document.text == "Rendered page body"
    assert selected.fetches == [url]


def test_http_fetch_preserves_failure_and_delegates_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    failure: dict[str, object] = {"status": "error", "url": "https://example.com/", "error": "page_blank"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: failure)
    result = WebSourceProvider().fetch("https://example.com/")
    assert {key: value for key, value in result.items() if key != "acquisition"} == failure
    assert result["acquisition"] == {"version": 1, "attempts": [
        attempt("http", "https://example.com/", failure),
    ]}
    sentinel = {"status": "ok", "content_kind": "pdf_download", "url": "https://example.com/a.pdf", "mime_type": "application/pdf"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: sentinel)
    requests: list[tuple[str, str]] = []

    def pdf(url: str, *, mime_type: str) -> dict[str, object]:
        requests.append((url, mime_type))
        return {"status": "ok", "text": "Extracted PDF"}

    monkeypatch.setattr("codey.research.source_download._download_pdf_streaming", pdf)
    assert WebSourceProvider().fetch("https://example.com/")["text"] == "Extracted PDF"
    assert requests == [("https://example.com/a.pdf", "application/pdf")]
