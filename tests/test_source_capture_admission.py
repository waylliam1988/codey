"""Captured DOM and fetched bodies share one source admission contract."""
from __future__ import annotations

import pytest

from codey.research.ledger import ResearchLedger
from codey.research.source_reading import render_source_read
from codey.types.json_value import JsonObject
from tools.browser_experiment_sources import BrowserCaptureGateway

URL = "https://example.com/article"


class CaptureProvider:
    def __init__(self, capture: dict[str, object]) -> None:
        self.capture = capture
        self.fetches = 0

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches += 1
        return self.capture


@pytest.mark.usefixtures("public_source_dns")
def test_capture_and_fetch_use_identical_receiving_and_rendering() -> None:
    provider = CaptureProvider({"status": "ok", "url": URL, "title": "Source", "text": "Original measured text."})
    fetched_ledger, captured_ledger = ResearchLedger(), ResearchLedger()
    fetched = BrowserCaptureGateway(provider, fetched_ledger).open(URL)
    captured = BrowserCaptureGateway(provider, captured_ledger).accept_capture(URL, provider.capture)
    fetched_read = render_source_read(fetched, fetched_ledger, offset=0, limit=6000)
    captured_read = render_source_read(captured, captured_ledger, offset=0, limit=6000)
    assert fetched_read.model_text == captured_read.model_text
    assert fetched_read.read_passages == captured_read.read_passages
    assert provider.fetches == 1


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("capture,expected", [
    ({"status": "error", "url": URL, "detail": "Restricted", "failure_code": "access_restricted"}, "access_restricted"),
    ({"status": "ok", "url": URL, "text": ""}, "page_not_loaded"),
    ({"status": "ok", "url": "http://127.0.0.1/private", "text": "Private"}, "policy_denied"),
])
def test_capture_failures_do_not_establish_reading(capture: dict[str, object], expected: str) -> None:
    provider = CaptureProvider(capture)
    ledger = ResearchLedger()
    output = render_source_read(BrowserCaptureGateway(provider, ledger).accept_capture(URL, capture), ledger,
                                offset=0, limit=6000)
    assert not output.ok
    assert output.failure_code == expected
    assert output.read_passages == ()
    assert not ledger.read_passages
    assert provider.fetches == 0
