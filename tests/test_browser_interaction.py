"""Real Chromium checks for semantic browser actions and stale refs."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import cast

import pytest
from playwright.sync_api import ElementHandle, Error, Page

from codey.research import browser_search
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import restore_ledger_observation
from codey.research.passages import restore_passages
from codey.research.source_reading import render_source_read
from codey.research.tools import ResearchTools
from codey.runtime.core import cancellation
from tests.support.real_cdp import chromium_executable
from tools.browser_experiment_interaction import BrowserInteraction
from tools.browser_experiment_sources import BrowserCaptureGateway, ResumableBrowserSearchProvider


class _InteractionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"<html><body><article>Initial readable article</article></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def browser_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ResumableBrowserSearchProvider, BrowserInteraction]]:
    server = HTTPServer(("127.0.0.1", 0), _InteractionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = ResumableBrowserSearchProvider(headless=True,
                                              browser_path=chromium_executable())

    def allow_fetch(_url: str, *, use_cache: bool = False) -> str:
        return ""

    monkeypatch.setattr(browser_search, "check_fetch_url", allow_fetch)
    url = f"http://127.0.0.1:{server.server_port}/article"
    try:
        assert provider.fetch(url)["status"] == "ok"
        yield provider, BrowserInteraction(provider)
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _install_page(provider: ResumableBrowserSearchProvider, html: str) -> None:
    def install() -> None:
        page = provider._fetch_page
        assert page is not None
        page.set_content(html)

    browser_search._search_browser_call(install)


def _ref(interaction: BrowserInteraction, role: str) -> str:
    snapshot = interaction.snapshot()
    assert snapshot.ok
    elements = snapshot.observation["elements"]
    assert isinstance(elements, list)
    return next(str(row["ref"]) for row in elements if isinstance(row, dict) and row["role"] == role)


def test_semantic_actions_use_snapshot_refs_and_reject_stale_refs(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider,
                  "<html><body><input aria-label='Query'><button aria-label='Load' "
                  "onclick=\"document.querySelector('#out').textContent='Loaded article'\">Load</button>"
                  "<div id='out'>Waiting</div><div style='height:3000px'></div></body></html>")
    first = interaction.snapshot()
    assert first.ok
    elements = first.observation["elements"]
    assert isinstance(elements, list)
    rows = [cast(dict[str, object], row) for row in elements if isinstance(row, dict)]
    query_ref = next(str(row["ref"]) for row in rows if row["role"] == "textbox")
    button_ref = next(str(row["ref"]) for row in rows if row["role"] == "button")
    assert interaction.fill(query_ref, "福州小吃").ok
    assert interaction.click(button_ref).ok is False

    second = interaction.snapshot()
    assert "Waiting" in str(second.observation["text"])
    second_elements = second.observation["elements"]
    assert isinstance(second_elements, list)
    second_rows = [cast(dict[str, object], row) for row in second_elements if isinstance(row, dict)]
    button_ref = next(str(row["ref"]) for row in second_rows if row["role"] == "button")
    assert interaction.click(button_ref).ok
    assert "Loaded article" in interaction.snapshot().model_text
    assert interaction.scroll("down", 700).ok
    assert interaction.wait("text", "Loaded article", 1000).ok
    assert interaction.click(button_ref).ok is False


@pytest.mark.parametrize("mutation", ["replace_node", "replace_document", "navigate_same_url"])
def test_old_node_cannot_target_replacement(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction], mutation: str,
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<button onclick='document.body.dataset.clicked=1'>Same name</button>")
    ref = _ref(interaction, "button")

    def mutate() -> None:
        page = cast(Page, provider._fetch_page)
        if mutation == "replace_node":
            page.evaluate("document.querySelector('button').outerHTML = document.querySelector('button').outerHTML")
        elif mutation == "replace_document":
            page.set_content("<button onclick='document.body.dataset.clicked=1'>Same name</button>")
        else:
            page.goto(page.url)

    browser_search._search_browser_call(mutate)
    rejected = interaction.click(ref)
    assert rejected.ok is False
    assert rejected.observation["delivery"] == "not_delivered"
    assert rejected.observation["reason"] == "stale_ref"


def test_ref_ids_are_not_reused_and_snapshots_report_truncation(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<button>A</button><button>B</button><div>" + "x" * 1000 + "</div>")
    old_ref = _ref(interaction, "button")
    snapshot = interaction.snapshot(max_elements=1, max_text=500)
    assert snapshot.observation["complete"] is False
    assert snapshot.observation["text_truncated"] is True
    assert snapshot.observation["elements_truncated"] is True
    assert interaction.click(old_ref).observation["delivery"] == "not_delivered"


def test_new_controller_cannot_reuse_reference_from_previous_controller(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<button>Original control</button>")
    old_ref = _ref(interaction, "button")
    replacement = BrowserInteraction(provider)
    new_ref = _ref(replacement, "button")
    assert old_ref != new_ref
    assert replacement.click(old_ref).observation["delivery"] == "not_delivered"


def test_wait_requires_visible_condition_and_propagates_cancel(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<div hidden>Secret</div><div id='later' hidden>Ready</div>"
                  "<script>setTimeout(() => document.querySelector('#later').hidden=false, 300)</script>")
    assert interaction.wait("text", "Secret", 100).ok is False
    assert interaction.wait("text", "Ready", 1000).ok
    event = threading.Event()
    event.set()
    with cancellation.scope(event), pytest.raises(cancellation.TaskCancelled):
        interaction.wait("text", "Never", 1000)


def test_cancel_during_wait_stops_browser_job(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    _, interaction = browser_fixture
    event = threading.Event()
    timer = threading.Timer(0.2, event.set)
    timer.start()
    started = time.monotonic()
    try:
        with cancellation.scope(event), pytest.raises(cancellation.TaskCancelled):
            interaction.wait("text", "Never appearing condition", 10000)
        assert time.monotonic() - started < 2
        assert interaction.snapshot().ok
    finally:
        timer.join(timeout=1)


def test_lost_click_acknowledgement_is_not_replayed(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction], monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<button onclick='window.count=(window.count || 0)+1'>Count</button>")
    ref = _ref(interaction, "button")
    original = ElementHandle.click

    def lose_ack(handle: ElementHandle, *, timeout: float, no_wait_after: bool) -> None:
        original(handle, timeout=timeout, no_wait_after=no_wait_after)
        raise Error("injected acknowledgement loss after real DOM click")

    monkeypatch.setattr(ElementHandle, "click", lose_ack)
    result = interaction.click(ref)
    assert not result.ok
    assert result.observation["delivery"] == "uncertain"
    assert interaction.click(ref).observation["delivery"] == "not_delivered"

    def count() -> object:
        return cast(Page, provider._fetch_page).evaluate("window.count")

    assert browser_search._search_browser_call(count) == 1


def test_snapshot_exposes_current_field_value_and_hides_password(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
) -> None:
    provider, interaction = browser_fixture
    _install_page(provider, "<label>Locality<input value='Fuzhou'></label>"
                  "<input type='password' aria-label='Password' value='private-secret'>")
    snapshot = interaction.snapshot()
    assert "Fuzhou" in snapshot.model_text
    assert "private-secret" not in str(snapshot)


def test_same_page_capture_preserves_old_version_and_restores_without_fetch(
    browser_fixture: tuple[ResumableBrowserSearchProvider, BrowserInteraction],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, interaction = browser_fixture
    def allow_fixture_url(_url: str, *, use_cache: bool = False) -> str:
        return ""

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", allow_fixture_url)
    ledger = ResearchLedger()
    gateway = BrowserCaptureGateway(provider, ledger)
    _install_page(provider, "<article>Original locality quantity is exactly 17 grams.</article>")
    first_capture = interaction.capture()
    url = str(first_capture["url"])
    first = render_source_read(gateway.accept_capture(url, first_capture), ledger, offset=0, limit=6000)
    assert first.ok
    _install_page(provider, "<article>Expanded locality quantity is exactly 99 grams.</article>")
    second_capture = interaction.capture()
    second = render_source_read(gateway.accept_capture(url, second_capture), ledger, offset=0, limit=6000)
    assert second.ok
    assert first.read_passages[0]["ref"] != second.read_passages[0]["ref"]
    tools = ResearchTools(search=provider, store=None, changes=None, ledger=ledger)
    reread = tools.read_source(first.read_observation)
    assert "17 grams" in reread.model_text
    assert "99 grams" not in reread.model_text
    provider.close()
    restored = ResearchLedger()
    for result in (first, second):
        restore_ledger_observation(restored, result.read_observation)
        restore_passages(restored, result.read_observation, result.read_passages)
    assert set(restored.read_passages) == set(ledger.read_passages)
    assert {row.source.text_hash for row in restored.read_passages.values()} == {
        row.source.text_hash for row in ledger.read_passages.values()}
