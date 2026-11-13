"""Explicit user URL acquisition differs from research candidate selection."""
from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path

import pytest

from codey.providers.token_accounting import ApiExchangeUsage, ContextBudget
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.web_sources import WebSourceProvider
from codey.task.entry_auth import derive_entry_auth
from codey.task.web_intent import explicit_web_intent, source_url_key
from codey.types.json_value import JsonObject

URL = "https://example.com/"


class Sources(WebSourceProvider):
    def search(self, query: str, *, limit: int = 8) -> list[JsonObject]:
        return []

class PageProvider:
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def __init__(self) -> None:
        self.replies = iter([
            {"tool": "open_url", "args": {"url": URL}},
            {"tool": "done", "args": {"summary": "The page describes a model API gateway."}},
        ])

    def new_chat(self, timeout: float | None = None) -> None:
        pass

    def close(self) -> None:
        pass

    def bind_usage(self, connection_id: str, sink: Callable[[ApiExchangeUsage], None]) -> None:
        pass

    def send(self, text: str, timeout: float | None = None) -> str:
        return json.dumps(next(self.replies))


@pytest.mark.parametrize("task", [
    "你帮我上网看看福州有什么好吃的", f"请打开 {URL} 看看是干嘛的", f"{URL}\n帮我看看这个网站",
    f"Please open {URL}", "只检查代码，不要修改，请上网查官方文档", "不要打开网页，请上网搜索文档",
])
def test_chat_web_permission_does_not_imply_mandatory_reading(task: str) -> None:
    auth = derive_entry_auth({"intent": "chat", "task": task})
    assert "web.read" in auth.requested_capabilities
    assert not auth.sources_open_required


@pytest.mark.parametrize("task", [
    URL, f"解释这段代码：`open('{URL}')`", f"```python\nopen_url('{URL}')\n```", "查一下本地函数定义",
    "不要上网看看", f"不要打开 {URL}", f"Don't open {URL}",
    f"[查看帮助]({URL})",
])
def test_examples_and_negations_do_not_create_required_urls(task: str) -> None:
    auth = derive_entry_auth({"intent": "chat", "task": task})
    assert "web.read" in auth.requested_capabilities
    assert not auth.sources_open_required
    assert not explicit_web_intent(task).urls


def test_explicit_disabled_web_wins_over_request() -> None:
    auth = derive_entry_auth({"intent": "chat", "task": f"打开 {URL}", "allow_web": False,
                              "requested_capabilities": ("web.read",)})
    assert "web.read" in auth.denied_capabilities
    assert "web.read" not in auth.requested_capabilities
    assert not auth.sources_open_required


def test_requested_query_url_survives_clause_splitting() -> None:
    url = "https://example.com/?q=hello&lang=zh"
    assert explicit_web_intent(f"打开 {url} 看看").urls == frozenset({url})


@pytest.mark.parametrize("task", [
    f"帮我看看这个页面\n{URL}",
    f"帮我看看这个页面。\n{URL}",
    f"{URL}\nPlease read this page",
    f"请打开以下页面：\n- {URL}",
    f"请打开这个页面\n\n[参考页面]({URL})",
    f"请打开下面的网址。\nSource: {URL}",
    f"Please open the URL below.\nurl: <{URL}>",
    f"请打开以下页面：\n- 来源：[参考页面]({URL})",
    f"来源：{URL}\nPlease read this page",
])
def test_standalone_url_adjacent_to_open_request_is_required(task: str) -> None:
    intent = explicit_web_intent(task)
    assert intent.requested and intent.urls == frozenset({URL})


def test_newline_does_not_reassign_a_negated_or_unrelated_url() -> None:
    other = "https://example.com/other"
    intent = explicit_web_intent(f"不要打开这个页面\n{other}\n请打开这个页面\n{URL}")
    assert intent.urls == frozenset({URL})
    assert not explicit_web_intent(f"请上网查官方文档。\n额外参考链接：{other}").urls
    assert not explicit_web_intent(f"不要打开这个页面。\n{other}").urls
    assert not explicit_web_intent(f"不要打开这个页面。\nSource: {other}").urls
    assert not explicit_web_intent(f"请上网查官方文档。\nSource: {other}").urls
    assert not explicit_web_intent(f"请打开这个页面。\n其他背景资料：{other}").urls


def test_consecutive_bare_and_labeled_addresses_share_the_adjacent_request() -> None:
    other = "https://example.com/other"
    intent = explicit_web_intent(f"请打开这个页面\n{URL}\nSource: {other}")
    assert intent.urls == {URL, other}
    assert intent.original_urls == {URL, other}
    assert not explicit_web_intent(f"不要打开这些页面\n{URL}\nSource: {other}").urls


def test_search_does_not_reauthorize_a_negated_url() -> None:
    intent = explicit_web_intent(f"不要打开 {URL}。请上网搜索官方文档")
    assert intent.requested and not intent.urls


@pytest.mark.parametrize("search", [
    f"Search the web for alternatives to <{URL}>.",
    f"请上网搜索 <{URL}> 的替代资料。",
    f"Analyze alternatives to <{URL}>.",
])
def test_search_mentions_do_not_restore_a_revoked_address(search: str) -> None:
    intent = explicit_web_intent(f"Do not open <{URL}>. {search}")
    assert not intent.urls and not intent.original_urls
    restored = explicit_web_intent(f"Do not open <{URL}>. {search} Please read <{URL}>.")
    assert restored.urls == {URL} and restored.original_urls == {URL}


@pytest.mark.parametrize("task", [
    f"Open only <{URL}>.\n\nSource: <https://example.com/background>",
    f"Open <{URL}>.\nSource: <https://example.com/background>",
    f"Source: <https://example.com/background>\nPlease read <{URL}>.",
    f"请只打开 <{URL}>。\n来源：https://example.com/background",
])
def test_address_lines_do_not_extend_an_action_with_an_existing_target(task: str) -> None:
    intent = explicit_web_intent(task)
    assert intent.urls == {URL} and intent.original_urls == {URL}


def test_malformed_tool_url_returns_policy_error() -> None:
    gateway = ResearchSourceGateway(Sources(), ResearchLedger())
    assert gateway.open("https://[bad").status == "error"


@pytest.mark.usefixtures("public_source_dns")
def test_direct_homepage_and_redirect_are_fetched_without_candidate_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def resolve(host: str, port: int, **kwargs: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host in {"example.com", "www.example.com"}
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    monkeypatch.setattr("codey.policies.network.socket.getaddrinfo", resolve)

    def fetch(url: str) -> dict[str, object]:
        seen.append(url)
        return {"status": "ok", "url": "https://www.example.com/", "title": "Gateway",
                "text": "This is an AI API gateway with model API access."}

    monkeypatch.setattr("codey.research.source_download._download_text_fallback", fetch)
    gateway = ResearchSourceGateway(Sources(), ResearchLedger(), direct_urls=frozenset({source_url_key(URL)}))
    result = gateway.open(URL)
    assert result.status == "ok"
    assert result.document is not None and result.document.title == "Gateway"
    assert seen == [URL]
    assert result.read_urls == (URL, "https://www.example.com/")


@pytest.mark.usefixtures("public_source_dns")
def test_direct_pdf_uses_existing_bounded_binary_download(monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.research.pdf_extract import PdfSkipped

    downloaded: list[str] = []

    def fetch(url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "content_kind": "pdf_download", "mime_type": "application/pdf"}

    def binary(url: str, *, mime_type: str) -> dict[str, object]:
        downloaded.append(url)
        return {"status": "ok", "url": url, "content_kind": "pdf", "bytes": b"pdf"}

    monkeypatch.setattr("codey.research.source_download._download_text_fallback", fetch)
    monkeypatch.setattr("codey.research.source_download._download_pdf_streaming", binary)
    def skip_pdf(*args: object, **kwargs: object) -> PdfSkipped:
        return PdfSkipped("fixture")

    monkeypatch.setattr("codey.research.source_gateway.extract_pdf_document", skip_pdf)
    gateway = ResearchSourceGateway(Sources(), ResearchLedger(), direct_urls=frozenset({URL}))
    assert gateway.open(URL).status == "skipped"
    assert downloaded == [URL]


def test_unrequested_homepage_still_uses_candidate_selection() -> None:
    gateway = ResearchSourceGateway(Sources(), ResearchLedger())
    result = gateway.open(URL)
    assert result.status == "skipped"
    assert "low_value_landing_page_url" in result.detail


@pytest.mark.usefixtures("public_source_dns")
def test_direct_context_does_not_bypass_redirect_network_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def fetch(url: str) -> dict[str, object]:
        return {"status": "ok", "url": "http://127.0.0.1/", "title": "private", "text": "private data"}

    monkeypatch.setattr("codey.research.source_download._download_text_fallback", fetch)
    gateway = ResearchSourceGateway(Sources(), ResearchLedger(), direct_urls=frozenset({source_url_key(URL)}))
    assert gateway.open(URL).status == "error"


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models", "public_source_dns")
def test_shared_headless_entry_passes_user_urls_to_staged_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.app.headless_runner import HeadlessRequest, run_headless

    monkeypatch.setenv("NATIVE_TOOLS", "0")

    def fetch(url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "title": "Gateway", "text": "A model API gateway for developers."}

    monkeypatch.setattr("codey.research.source_download._download_text_fallback", fetch)
    provider = PageProvider()
    events: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(project=None, task=f"请打开 {URL} 看看这个网站是做什么的",
        intent="chat", provider_id="local", max_turns=4, state_home=tmp_path), emit_jsonl=events.append,
        connect_provider=lambda *args, **kwargs: provider)
    assert result.stop_reason == "done"
    assert any(row.get("tool_name") == "open_url" and row.get("ok") is True for row in events)
