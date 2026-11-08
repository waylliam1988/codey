"""Ordinary chat uses bounded HTTP without starting Research's browser."""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from codey.operations.task_execution import build_research_tools
from codey.research.chat_web import ChatWebProvider


def test_search_preserves_chinese_query_and_parses_actual_rss(monkeypatch):
    requests = []

    def read(url, *, timeout):
        requests.append((url, timeout))
        return ("<rss><channel><item><title>福州小吃</title><link>https://example.com/food?a=1&amp;b=2</link>"
                "<description>&lt;b&gt;鱼丸&lt;/b&gt;、肉燕与锅边糊</description></item>"
                "<item><title>Second</title><link>https://example.com/second</link></item></channel></rss>")

    monkeypatch.setattr("codey.research.chat_web._read_url_text", read)
    query = "福州 美食 肉燕+鱼丸 & 地方小吃"
    results = ChatWebProvider().search(query, limit=1)
    assert parse_qs(urlsplit(requests[0][0]).query) == {"format": ["rss"], "q": [query]}
    assert requests[0][1] == 12
    assert results == [{"title": "福州小吃", "url": "https://example.com/food?a=1&b=2",
                        "snippet": "鱼丸、肉燕与锅边糊"}]


@pytest.mark.parametrize("document", ["<rss>", "<html><body>Verify you are human</body></html>", "<rss />"])
def test_challenges_or_invalid_documents_are_failures(monkeypatch, document):
    monkeypatch.setattr("codey.research.chat_web._read_url_text", lambda *a, **kw: document)
    with pytest.raises(ValueError, match="HTTP search"):
        ChatWebProvider().search("福州美食")


def test_empty_rss_is_distinct_from_acquisition_failure(monkeypatch):
    monkeypatch.setattr("codey.research.chat_web._read_url_text", lambda *a, **kw: "<rss><channel /></rss>")
    assert ChatWebProvider().search("福州美食") == []


def test_chat_resources_never_construct_browser_and_keep_injected_provider(monkeypatch):
    def browser():
        pytest.fail("ordinary chat constructed Research browser")

    monkeypatch.setattr("codey.research.search_factory.default_research_search_provider", browser)
    resources = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=None),
                                     session_id="s", project="", browser_sources=False)
    assert isinstance(resources.search, ChatWebProvider)
    injected = ChatWebProvider()
    resources = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=lambda: injected),
                                     session_id="s", project="", browser_sources=False)
    assert resources.search is injected


@pytest.mark.parametrize("browser_sources", [False, True])
def test_explicit_factory_is_respected_for_both_chat_and_research(monkeypatch, browser_sources):
    from codey.research.ledger import ResearchLedger
    from codey.research.source_gateway import ResearchSourceGateway

    calls = []

    class Provider:
        def search(self, query: str, *, limit: int):
            return []

        def fetch(self, url: str):
            calls.append(url)
            return {"status": "ok", "url": url, "title": "Configured", "text": "Configured provider body"}

    selected = Provider()
    tools = build_research_tools(SimpleNamespace(knowledge_store=None, search_factory=lambda: selected),
        session_id="s", project="", browser_sources=browser_sources)
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **k: None)
    url = "https://example.com/"
    gateway = ResearchSourceGateway(tools.search, ResearchLedger(), direct_urls=frozenset({url}))
    result = gateway.open(url)
    assert result.status == "ok" and result.document.text == "Configured provider body"
    assert calls == [url]


def test_http_fetch_preserves_failure_and_delegates_pdf(monkeypatch):
    failure = {"status": "error", "url": "https://example.com/", "error": "page_blank"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: failure)
    result = ChatWebProvider().fetch("https://example.com/")
    assert {key: value for key, value in result.items() if key != "acquisition"} == failure
    assert result["acquisition"]["attempts"][0]["backend"] == "http"
    sentinel = {"content_kind": "pdf_download", "url": "https://example.com/a.pdf", "mime_type": "application/pdf"}
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", lambda url: sentinel)
    requests = []

    def pdf(url, *, mime_type):
        requests.append((url, mime_type))
        return {"status": "ok", "text": "Extracted PDF"}

    monkeypatch.setattr("codey.research.source_download._download_pdf_streaming", pdf)
    assert ChatWebProvider().fetch("https://example.com/")["text"] == "Extracted PDF"
    assert requests == [("https://example.com/a.pdf", "application/pdf")]
