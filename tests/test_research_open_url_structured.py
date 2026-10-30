"""Structured open_url migration: window vs receipt, sentinel, fail-closed runner."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.tools import ResearchToolOutput, ResearchTools


class _LongSearch:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def fetch(self, requested: str) -> dict:
        return {"status": "ok",
            "url": requested,
            "title": "Long doc",
            "text": self.text,
            "truncated": False,
        }


@pytest.mark.usefixtures("public_source_dns")
def test_open_url_returns_structured_window_and_full_receipt() -> None:
    url = "https://example.com/long"
    prefix = "P" * 6000
    needle = "NEEDLE_" * 200  # 1400 chars inside the requested window
    window_pad = "W" * (2000 - len(needle))
    tail = "T" * 20000 + "TAIL_ONLY_MARKER_END"
    full_text = prefix + needle + window_pad + tail
    assert len(full_text) > 28000

    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td))
        tools = ResearchTools(_LongSearch(full_text, url), store, KnowledgeChanges(store.root))
        opened = tools.open_url(url, offset=6000, limit=2000)
        text_view = tools.open_url(url, offset=6000, limit=2000).model_text
        store.close()

    assert isinstance(opened, ResearchToolOutput)
    expected_window = full_text[6000:8000]
    # Labels delimit exact subpassages without changing their original bytes.
    assert "".join(full_text[row["start"]:row["end"]] for row in opened.read_passages) == expected_window
    assert "NEEDLE_" in opened.model_text
    # Window must not leak the far tail.
    assert "TAIL_ONLY_MARKER_END" not in opened.model_text
    # Receipt keeps the full rendered text.
    assert opened.receipt_text
    assert len(opened.receipt_text) > len(opened.model_text)
    assert "TAIL_ONLY_MARKER_END" in opened.receipt_text
    assert isinstance(text_view, str)
    assert text_view == opened.model_text


def test_receipt_externalize_keeps_window_only_for_model() -> None:
    from codey.research.output_receipts import maybe_externalize_output

    full = "WINDOW\n" + "x" * 30000 + "\nTAIL_ONLY"
    call = SimpleNamespace(name="open_url", args={"url": "https://example.com"})
    outcome = maybe_externalize_output(
        store=None,
        session_id="",
        run_id="",
        permission_profile="research",
        call=call,
        output=full,
        turn=1,
        tool_index=0,
        model_text_override="WINDOW",
        ok=True,
    )
    assert "WINDOW" in outcome.model_text
    assert "TAIL_ONLY" not in outcome.model_text
    assert outcome.truncated is True


def test_receipt_override_skips_head_tail_clip() -> None:
    from types import SimpleNamespace as _NS

    from codey.research import output_receipts as receipts

    calls: list[str] = []
    original = receipts.head_tail_clip

    def _spy(text: str):  # type: ignore[no-untyped-def]
        calls.append(text)
        return original(text)

    receipts.head_tail_clip = _spy  # type: ignore[method-assign]
    try:
        full = "WINDOW\n" + "x" * 30000 + "\nTAIL_ONLY"
        call = _NS(name="open_url", args={"url": "https://example.com"})
        outcome = receipts.maybe_externalize_output(
            store=None,
            session_id="",
            run_id="",
            permission_profile="research",
            call=call,
            output=full,
            turn=1,
            tool_index=0,
            model_text_override="WINDOW",
            ok=True,
        )
    finally:
        receipts.head_tail_clip = original  # type: ignore[method-assign]
    assert calls == []
    assert outcome.model_text == "WINDOW"


def test_research_tool_output_is_window_plus_receipt_only() -> None:
    import dataclasses

    names = {field.name for field in dataclasses.fields(ResearchToolOutput)}
    assert names == {"model_text", "receipt_text", "read_passages", "read_observation", "ok"}


@pytest.mark.usefixtures("public_source_dns")
def test_source_eof_is_explicit_and_out_of_range_reads_do_not_commit_observations(tmp_path):
    from codey.runtime.core.models import ToolCall
    from tests.test_research_findings import URL, execute, setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Previous source content. " * 1400 + "FINAL_FACT"
        size = len(tools.search.text)
        for offset in (size, size + 8000):
            result, ok, _ = delegate._execute_research(ToolCall("open_url", {"url": URL, "offset": offset}))
            assert not ok and not result.ok
            assert f"length={size} characters" in result.model_text
            assert "No passage was read" in result.model_text
            assert not result.canonical.get("research_observation") and not result.canonical.get("read_passages")
        assert not tools.ledger.opened_sources and not tools.ledger.read_passages and not session.opened_sources
        result = execute(delegate, tools, session, "open_url", {"url": URL, "offset": size - 400, "limit": 500})
        assert "FINAL_FACT" in result.model_text
        assert f"end={size} (exclusive), available_length={size} characters" in result.model_text
        assert "End of available extracted source text" in result.model_text
        assert "more text available" not in result.model_text
        assert tools.ledger.read_passages
    finally:
        store.close()
