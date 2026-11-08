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

from codey.operations.kernel_execution import execute_turn
from codey.operations.task_session import TaskSession
from codey.research import browser_search, source_download
from codey.research.chat_web import ChatWebProvider
from codey.research.extract import is_application_shell
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
    assert result["status"] == "error" and result["render_required"] is True
    assert response.closed


@pytest.mark.parametrize("page", [
    {"status": "error", "detail": "page_blank"},
    {"status": "error", "detail": "HTTP 403"},
    {"status": "skipped", "content_kind": "pdf"},
    {"status": "ok", "text": BODY},
])
def test_no_browser_for_non_shell_results(monkeypatch: pytest.MonkeyPatch, page: dict[str, object]) -> None:
    factory = Mock(side_effect=AssertionError("unnecessary browser"))
    monkeypatch.setattr("codey.research.chat_web.fetch_http_document", lambda _: page)
    provider = ChatWebProvider(renderer_factory=factory)
    provider.fetch("https://example.com/")
    provider.close()
    factory.assert_not_called()


@pytest.mark.parametrize("error", [cancellation.TaskCancelled, cancellation.DeadlineExceeded])
def test_interruptions_never_trigger_rendering(monkeypatch: pytest.MonkeyPatch, error: type[Exception]) -> None:
    def interrupted(url: str) -> dict[str, object]:
        raise error()

    monkeypatch.setattr("codey.research.chat_web.fetch_http_document", interrupted)
    factory = Mock(side_effect=AssertionError("rendered after interruption"))
    with pytest.raises(error):
        ChatWebProvider(renderer_factory=factory).fetch("https://example.com/")


def test_http_and_rendering_share_earlier_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    deadlines: list[float | None] = []

    def http(url: str) -> dict[str, object]:
        deadlines.append(cancellation.current_deadline())
        return {"status": "error", "render_required": True}

    class Renderer:
        def fetch(self, url: str) -> dict[str, object]:
            deadlines.append(cancellation.current_deadline())
            return {"status": "ok", "text": BODY}

        def close(self) -> None:
            pass

    monkeypatch.setattr("codey.research.chat_web.fetch_http_document", http)
    deadline = time.monotonic() + 5
    with cancellation.deadline_scope(deadline):
        ChatWebProvider(renderer_factory=Renderer).fetch("https://example.com/")
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
    provider = ChatWebProvider()
    tools.search = provider
    tools.direct_urls = frozenset({spa_server})
    monkeypatch.setattr(source_download, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    try:
        result = execute_turn(session, [ToolCall("open_url", {"url": spa_server})], run_id="r", turn=1,
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
        assert ledger.source_text_for_url(spa_server) == BODY
    finally:
        provider.close()
        sink.durable.close()


def test_render_only_failure_has_no_reverse_http(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = browser_search.BrowserSearchProvider(http_fallback=False)
    page = Mock(url="https://example.com/")
    page.goto.return_value = None
    page.evaluate.return_value = True
    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", lambda _: page)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr(browser_search, "_fetch_page_content_after_settle", lambda _: (SHELL, ""))
    monkeypatch.setattr(source_download, "_download_text_fallback", Mock(side_effect=AssertionError("repeated HTTP")))
    result = provider._fetch_on_browser_thread(page.url)
    assert result["status"] == "error" and "render_timeout" in str(result["detail"])


def test_headless_guard_covers_context_including_popup_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Mock()
    monkeypatch.setattr(browser_search, "open_headless_page", lambda **kwargs: session)
    provider = browser_search.BrowserSearchProvider(headless=True, http_fallback=False)
    assert provider._ensure_session_on_browser_thread() is session
    session.page.context.route.assert_called_once_with("**/*", provider._guard_request)
    provider._prepare_page_on_browser_thread(session.page)
    session.page.route.assert_not_called()
    provider._close_on_browser_thread()
    session.page.context.unroute_all.assert_called_once_with(behavior="wait")
    session.close.assert_called_once()


def test_abandoned_headless_fetch_closes_owned_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = browser_search.BrowserSearchProvider(headless=True, http_fallback=False)
    closed = Mock()
    monkeypatch.setattr(provider, "_close_on_browser_thread", closed)
    provider._abandon_fetch_on_browser_thread()
    closed.assert_called_once()


@pytest.mark.parametrize("text", ["Verify you are human", "Sign in to continue with your account"])
def test_challenge_or_login_after_render_is_not_evidence(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    provider = browser_search.BrowserSearchProvider(http_fallback=False)
    page = Mock(url="https://example.com/")
    page.goto.return_value = None
    page.evaluate.return_value = False
    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", lambda _: page)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr(browser_search, "_fetch_page_content_after_settle", lambda _: ("<body>" + text + "</body>", text))
    result = provider._fetch_on_browser_thread(page.url)
    assert result["status"] == "error"
    assert "page_unavailable" in str(result["detail"]) or "login_required" in str(result["detail"])


@pytest.mark.parametrize(("event", "expected"), [("pageerror", "render_script_error"),
                                                   ("requestfailed", "render_request_failed")])
def test_failed_render_reports_observed_script_or_request_error(
    monkeypatch: pytest.MonkeyPatch, event: str, expected: str,
) -> None:
    from types import SimpleNamespace

    page = Mock(url="https://example.com/")
    handlers = {}
    page.on.side_effect = lambda name, handler: handlers.update({name: handler})

    def navigate(*args: object, **kwargs: object) -> None:
        handlers[event](SimpleNamespace(message="JavaScript failed", resource_type="fetch", failure="connection refused"))

    page.goto.side_effect = navigate
    provider = browser_search.BrowserSearchProvider(http_fallback=False)
    monkeypatch.setattr(provider, "_prepare_fetch_page_on_browser_thread", lambda _: page)
    monkeypatch.setattr(browser_search, "check_fetch_url", lambda *a, **k: "")
    monkeypatch.setattr(browser_search, "_fetch_page_content_after_settle", lambda _: (SHELL, ""))
    result = provider._fetch_on_browser_thread(page.url)
    assert expected in str(result["detail"])
    assert page.remove_listener.call_count == 3
