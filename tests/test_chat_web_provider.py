"""Ordinary chat searches the real browser and retains HTTP document acquisition."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.operations.task_execution import build_research_tools
from codey.research import browser_search, connector_search
from codey.research.chat_web import ChatWebProvider
from codey.runtime.core import cancellation
from codey.types.json_value import JsonObject


class Browser:
    def __init__(self) -> None:
        self.searches: list[tuple[str, int]] = []
        self.fetches: list[str] = []
        self.closed = 0
        self.hits: list[JsonObject] = [{"title": "福州小吃", "url": "https://example.com/food?a=1&b=2",
                                       "snippet": "鱼丸、肉燕与锅边糊"}]

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        self.searches.append((query, limit))
        return self.hits

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches.append(url)
        return {"status": "ok", "url": url, "title": "Rendered", "text": "Rendered page body"}

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("query", ["福州 美食 肉燕+鱼丸 & 地方小吃", "东百中心附近有什么好玩的", "东百中心有什么好玩的"])
def test_default_search_uses_persistent_headed_browser_and_preserves_query(
    monkeypatch: pytest.MonkeyPatch, query: str,
) -> None:
    browser = Browser()
    configurations: list[tuple[bool, bool, bool]] = []

    def create(*, headless: bool, isolated: bool, http_fallback: bool) -> Browser:
        configurations.append((headless, isolated, http_fallback))
        return browser

    def forbidden(url: str, *, timeout: float) -> str:
        pytest.fail("browser search must not request HTTP/RSS search")

    monkeypatch.setattr(browser_search, "BrowserSearchProvider", create)
    monkeypatch.setattr(connector_search, "_read_url_text", forbidden)
    provider = ChatWebProvider()
    assert configurations == []
    assert provider.search(query, limit=3) == browser.hits
    assert configurations == [(False, False, False)]
    assert browser.searches == [(query, 3)]
    provider.close()
    assert browser.closed == 1


@pytest.mark.parametrize("error", [ValueError("search challenge"), cancellation.TaskCancelled(), cancellation.DeadlineExceeded()])
def test_search_failure_and_interruption_propagate_without_http_fallback(
    monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    browser = Browser()

    def fail(query: str, *, limit: int) -> list[JsonObject]:
        raise error

    monkeypatch.setattr(browser, "search", fail)
    provider = ChatWebProvider(browser_factory=lambda: browser)
    with pytest.raises(type(error)):
        provider.search("福州美食")
    provider.close()
    assert browser.closed == 1


def test_search_and_render_share_browser_but_static_reads_do_not_start_it(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = Browser()
    created: list[Browser] = []

    def create() -> Browser:
        created.append(browser)
        return browser

    provider = ChatWebProvider(browser_factory=create)
    monkeypatch.setattr("codey.research.chat_web.fetch_http_document", lambda url: {"status": "ok", "text": "Static"})
    assert provider.fetch("https://example.com/static")["text"] == "Static"
    assert created == []
    browser.hits = []
    assert provider.search("empty search") == []
    monkeypatch.setattr("codey.research.chat_web.fetch_http_document", lambda url: {"status": "error", "render_required": True})
    assert provider.fetch("https://example.com/spa")["text"] == "Rendered page body"
    assert created == [browser]
    assert browser.fetches == ["https://example.com/spa"]
    provider.close()
    provider.close()
    assert browser.closed == 1


def test_chat_resources_select_chat_provider_lazily_and_keep_injected_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    def research() -> None:
        pytest.fail("ordinary chat constructed the strict Research provider")

    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", research)
    resources = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=None),
                                     session_id="s", project="", browser_sources=False)
    assert isinstance(resources.search, ChatWebProvider)
    injected = ChatWebProvider()
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
    failure = {"status": "error", "url": "https://example.com/", "error": "page_blank"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: failure)
    result = ChatWebProvider().fetch("https://example.com/")
    assert {key: value for key, value in result.items() if key != "acquisition"} == failure
    assert result["acquisition"] == {"version": 1, "attempts": [
        {"backend": "http", "requested_url": "https://example.com/", "final_url": "https://example.com/", "status": "error"},
    ]}
    sentinel = {"content_kind": "pdf_download", "url": "https://example.com/a.pdf", "mime_type": "application/pdf"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: sentinel)
    requests: list[tuple[str, str]] = []

    def pdf(url: str, *, mime_type: str) -> dict[str, object]:
        requests.append((url, mime_type))
        return {"status": "ok", "text": "Extracted PDF"}

    monkeypatch.setattr("codey.research.source_download._download_pdf_streaming", pdf)
    assert ChatWebProvider().fetch("https://example.com/")["text"] == "Extracted PDF"
    assert requests == [("https://example.com/a.pdf", "application/pdf")]
