"""One bounded acquisition owns HTTP, rendering and its durable receipt."""
from __future__ import annotations

import io
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.research import source_download
from codey.research.extract import is_application_shell
from codey.research.search_contracts import SearchOutcome
from codey.research.source_acquisition import with_attempt
from codey.research.web_sources import WebSourceProvider
from codey.runtime.core import cancellation
from codey.runtime.core.models import ToolCall
from codey.types.json_value import decode_json_object
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_committed_research_projection import runtime

SHELL = '<html><title>Application</title><body><div id="root"></div><script src="/app.js"></script></body></html>'
BODY = "This application provides a verified document search service with public pricing."


@pytest.mark.parametrize(("html", "expected"), [
    (SHELL, True),
    ('<div id="root"><div><main>Loading...</main></div></div><script type="module">boot()</script>', True),
    ('<div id="app"><nav>Navigation with substantial text</nav><main></main></div><script src="/app.js"></script>', True),
    ('<div id="root"><div>Actual application content is already present.</div></div><script src="/app.js"></script>', False),
    ('<div id="root"></div>', False),
    ('<body>Short</body><script src="analytics.js"></script>', False),
    ('<main></main><script src="analytics.js"></script>', False),
])
def test_shell_requires_bootstrap_and_unfilled_application_region(html: str, expected: bool) -> None:
    assert is_application_shell(html) is expected


@pytest.mark.parametrize("html", [SHELL, SHELL.replace('<div id="root"></div>',
    '<nav>Navigation with enough words to look like useful content.</nav><div id="root"></div>')])
def test_http_reports_shell_even_when_navigation_is_long(monkeypatch: pytest.MonkeyPatch, html: str) -> None:
    response = io.BytesIO(html.encode())
    response.status = 200
    response.headers = {"content-type": "text/html"}
    response.url = "https://example.com/"
    monkeypatch.setattr(source_download, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr(source_download, "_open_url_no_redirect", lambda *a, **k: response)
    result = source_download.fetch_http_document(response.url)
    assert result["status"] == "error" and result["failure_code"] == "page_not_loaded" and result["http_status"] == 200
    assert response.closed


@pytest.mark.parametrize("page", [
    {"status": "error", "failure_code": "policy_denied", "detail": "page_blank"},
    {"status": "error", "http_status": 404, "failure_code": "page_unavailable", "detail": "HTTP 403"},
    {"status": "skipped", "content_kind": "pdf"},
    {"status": "ok", "text": BODY},
])
def test_no_browser_for_terminal_or_successful_results(monkeypatch: pytest.MonkeyPatch, page: dict[str, object]) -> None:
    factory = Mock(side_effect=AssertionError("unnecessary browser"))
    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", lambda _: page)
    provider = WebSourceProvider(browser_factory=factory)
    provider.fetch("https://example.com/")
    provider.close()
    factory.assert_not_called()


@pytest.mark.parametrize("error", [cancellation.TaskCancelled, cancellation.DeadlineExceeded])
def test_interruptions_never_trigger_rendering(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    def interrupted(url: str) -> dict[str, object]:
        raise error()

    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", interrupted)
    factory = Mock(side_effect=AssertionError("rendered after interruption"))
    with pytest.raises(error):
        WebSourceProvider(browser_factory=factory).fetch("https://example.com/")


def test_http_and_rendering_share_earlier_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    deadlines: list[float | None] = []

    def http(url: str) -> dict[str, object]:
        deadlines.append(cancellation.current_deadline())
        return with_attempt({"status": "error", "failure_code": "page_not_loaded"}, "http", url)

    class Renderer:
        last_search_errors: list[dict[str, str]] = []

        def search(self, query: str, *, limit: int) -> SearchOutcome:
            pytest.fail("page acquisition must not search")

        def fetch(self, url: str) -> dict[str, object]:
            deadlines.append(cancellation.current_deadline())
            return {"status": "ok", "text": BODY}

        def close(self) -> None:
            pass

    monkeypatch.setattr("codey.research.web_sources.fetch_http_document", http)
    deadline = time.monotonic() + 5
    with cancellation.deadline_scope(deadline):
        WebSourceProvider(browser_factory=Renderer).fetch("https://example.com/")
    assert deadlines == [deadline, deadline]


@pytest.fixture
def spa_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = ("setTimeout(() => document.querySelector('#root').innerText = " + repr(BODY) + ", 50);"
                    if self.path == "/app.js" else SHELL)
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path.endswith(".js") else "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_headless_spa_and_receipt_recovery_never_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spa_server: str,
) -> None:
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)
    from contextlib import AbstractContextManager
    from http.client import HTTPResponse
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen
    from urllib.response import addinfourl

    from codey.automation.browser_contracts import RouteDecision
    from codey.research.task_browser_source import TaskBrowserSource
    from tests.support.task_browser import browser_for

    public_url = "https://spa.example.test/"
    def open_fixture(request: Request, *, timeout: float) -> addinfourl:
        opened: AbstractContextManager[HTTPResponse] = urlopen(spa_server + urlsplit(request.full_url).path.lstrip("/"), timeout=timeout)
        with opened as response:
            return addinfourl(io.BytesIO(response.read()), response.headers, request.full_url, response.status)
    def render_route(reply: RouteDecision) -> None:
        opened = open_fixture(Request(reply.request.url), timeout=3)
        with opened:
            reply.fulfill(status=opened.code, headers=dict(opened.headers.items()), body=opened.read())
    browser = browser_for(render_route)
    renderer = TaskBrowserSource(browser, owns_browser=True)
    provider = WebSourceProvider(browser_factory=lambda: renderer)
    tools.search = provider
    tools.direct_urls = frozenset({public_url})
    def allow_fixture(url: str, *, resolve: bool = True, use_cache: bool = False) -> None:
        return None
    monkeypatch.setattr(source_download, "check_fetch_url", allow_fixture)
    monkeypatch.setattr(source_download, "_open_url_no_redirect", open_fixture)
    try:
        result = execute_turn(session, [ToolCall("open_url", {"url": public_url})], run_id="r", turn=1,
            effect_scope="task", research_tools=tools, managed_outputs=managed,
            session_id="s", intent_sink=sink)[0]
        assert result.ok, result.model_text
        assert BODY in result.model_text
        trace = decode_json_object(result.canonical["source_acquisition"])
        assert [decode_json_object(row)["backend"] for row in trace["attempts"]] == ["http", "browser"]
        assert "application shell" in str(trace)
        provider.close()
        monkeypatch.setattr(provider, "fetch", Mock(side_effect=AssertionError("recovery fetched again")))
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="chat")
        assert recovered.ok
        restored = TaskSession(session.policy)
        from types import SimpleNamespace

        from codey.research.ledger import ResearchLedger

        ledger = ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes)
        restore_task_session(frame, restored, research_ledger=ledger)
        assert next(iter(restored._memory_results.values())).canonical["source_acquisition"] == trace
        assert ledger.source_text_for_url(public_url) == BODY
    finally:
        provider.close()
        sink.durable.close()
