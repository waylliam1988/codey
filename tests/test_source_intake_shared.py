"""Fresh, captured and restored source reads share receipt and passage generation."""
from __future__ import annotations

import pytest

from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import restore_ledger_observation, source_observation
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.source_reading import render_source_read
from codey.research.tools import ResearchTools
from codey.types.json_value import JsonObject
from tests.test_research import fake_pypdf

URL = "https://example.com/field-record"


class StaticSource:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.calls = 0

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        self.calls += 1
        return {"status": "ok", "url": url, "title": "Field record", "content_kind": self.kind,
                "text": "A measured field result.\n\n" * 100, "bytes": b"%PDF fixture"}


@pytest.mark.parametrize("kind", ["html", "abstract", "pdf"])
def test_capture_has_one_timestamp_and_identical_read_output(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    stamps: list[str] = []

    def now() -> str:
        stamp = f"2026-11-09T01:00:{len(stamps):02d}+00:00"
        stamps.append(stamp)
        return stamp

    monkeypatch.setattr("codey.research.ledger.now_iso", now)
    provider = StaticSource(kind)
    tools = ResearchTools(provider, None, None)
    with fake_pypdf("A measured result on page one.", "A separate measured result on page two."):
        first = tools.open_url(URL)
    assert first.ok and len(stamps) == 1
    source, _text, _pages = source_observation(first.read_observation)
    assert source.retrieved_at == stamps[0]
    assert tools.ledger.source_record_for_url(URL) == source
    captured_tools = ResearchTools(None, None, None)
    captured = captured_tools.read_source(first.read_observation)
    restored = ResearchLedger()
    restore_ledger_observation(restored, first.read_observation)
    restored_tools = ResearchTools(None, None, None, ledger=restored)
    replayed = restored_tools.read_source(first.read_observation)
    assert len(stamps) == provider.calls == 1
    assert first == captured == replayed
    assert tools.ledger.read_passages == captured_tools.ledger.read_passages == restored_tools.ledger.read_passages


def test_gateway_pdf_receipt_preserves_this_acquisition_before_projection_union(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", lambda *a, **kw: None)
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(StaticSource("pdf"), ledger)
    with fake_pypdf("First original page.", "Second original page."):
        assert gateway.open(URL, pages="1").status == "ok"
        second = gateway.open(URL, pages="2")
    source, text, pages = source_observation(second.observation)
    assert source.pages_read == (2,) and set(pages) == {2}
    assert text == "[page 2]\nSecond original page."
    assert ledger.pages_read_for_url(URL) == {1, 2}
    output = render_source_read(second, ResearchLedger(), offset=0, limit=6000)
    assert output.ok and output.read_observation == second.observation
    assert "First original page" not in output.model_text
