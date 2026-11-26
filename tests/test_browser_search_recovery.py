"""Resource bursts, cancellation and shared DNS on the production runtime."""
from __future__ import annotations

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest

from codey.automation.browser_contracts import RouteDecision
from codey.policies.network import NetworkPolicy, check_browser_url
from codey.research.search_contracts import SearchState
from codey.research.task_browser_source import TaskBrowserSource
from codey.runtime.core import cancellation
from tests.support.search_fixture import FORM, RESULT, SearchFixture
from tests.support.task_browser import browser_for


def test_resource_burst_above_tool_command_limit_does_not_abort_page_assets() -> None:
    fixture = SearchFixture()
    assets: list[str] = []
    def route(reply: RouteDecision) -> None:
        parsed = urlsplit(reply.request.url)
        if parsed.hostname == "cdn.example.com":
            assets.append(parsed.path)
            time.sleep(0.015)
            reply.fulfill(content_type="application/javascript", body="window.loaded++; if(window.loaded===96) document.querySelector('#b_results').innerHTML=" + json.dumps(RESULT) + ";")
        elif parse_qs(parsed.query).get("q"):
            body = FORM.format(query="burst") + "<ol id='b_results'></ol><script>window.loaded=0</script>"
            body += "".join(f"<script defer src='https://cdn.example.com/{i}.js'></script>" for i in range(96))
            reply.fulfill(content_type="text/html", body=body)
        else:
            fixture.route(reply)
    browser = browser_for(route)
    try:
        outcome = TaskBrowserSource(browser, engine="bing").search("burst", deadline=time.monotonic() + 12)
        assert outcome.state == SearchState.HITS, outcome
        assert len(assets) == 96
        assert not any(event.get("kind") == "blocked" for event in browser.diagnostics())
    finally:
        browser.close()


def test_concurrent_same_origin_checks_share_one_system_dns_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    def resolve(host: str, port: int, *, proto: int) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        calls.append(host)
        time.sleep(0.1)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    policy = NetworkPolicy()
    def check(index: int) -> str | None:
        return policy.check_url(f"https://example.com/{index}.js", use_cache=True)
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert list(workers.map(check, range(16))) == [None] * 16
    assert calls == ["example.com"]


@pytest.mark.parametrize("url,kind,navigation,allowed", [
    ("data:image/png;base64,AA==", "image", False, True),
    ("blob:https://example.com/asset", "image", False, True),
    ("data:text/html,document", "document", True, False),
    ("file:///C:/private", "image", False, False),
    ("http://127.0.0.1/private", "xhr", False, False),
    ("http://192.168.1.1/", "document", True, False),
])
def test_inline_resource_exception_does_not_open_private_network(url: str, kind: str, navigation: bool, allowed: bool) -> None:
    assert (check_browser_url(url, kind, navigation) is None) is allowed


def test_cancelled_search_does_not_retry_or_poison_next_query() -> None:
    fixture = SearchFixture()
    browser = browser_for(fixture.route)
    timer: threading.Timer | None = None
    try:
        source = TaskBrowserSource(browser, engine="bing")
        assert source.search("warm").state == SearchState.HITS
        fixture.mode = "shell"
        stop = threading.Event()
        timer = threading.Timer(0.5, stop.set)
        timer.start()
        with cancellation.scope(stop), pytest.raises(cancellation.TaskCancelled):
            source.search("cancelled")
        fixture.mode = "hits"
        assert source.search("recovered").state == SearchState.HITS
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        browser.close()
