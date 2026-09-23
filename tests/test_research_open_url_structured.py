"""Structured open_url migration: window vs receipt, sentinel, fail-closed runner."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.research.tools import ResearchToolOutput, ResearchTools


class _LongSearch:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def fetch(self, requested: str) -> dict:
        return {
            "url": requested,
            "title": "Long doc",
            "text": self.text,
            "truncated": False,
        }


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
        text_view = tools.open_url_text(url, offset=6000, limit=2000)
        store.close()

    assert isinstance(opened, ResearchToolOutput)
    expected_window = full_text[6000:8000]
    assert expected_window in opened.model_text
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
    )
    assert "WINDOW" in outcome.model_text
    assert "TAIL_ONLY" not in outcome.model_text
    assert outcome.truncated is True


def test_runner_open_url_fail_closed_on_string() -> None:
    import pytest

    from codey.research.runner import ResearchRunner
    from codey.runtime.core.models import ToolCall

    class _Search:
        last_connector_errors: list = []

    class _Provider:
        name = "test"

        def new_chat(self, timeout=None) -> None:
            return None

    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td))
        runner = ResearchRunner(_Provider(), _Search(), store, session_id="s", run_id="r")
        runner.tools.open_url = lambda *a, **k: "legacy string"  # type: ignore[method-assign]
        with pytest.raises(TypeError, match="must return ResearchToolOutput"):
            runner._dispatch(ToolCall("open_url", {"url": "https://example.com"}), 1, 0)
        store.close()
