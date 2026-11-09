"""Live and restored source locators read captured versions without new acquisition."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.research.source_snapshots import captured_source
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.storage.managed_outputs import ManagedOutputStore
from codey.types.json_value import JsonObject
from tests.support.provider_actions import ProviderActions
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_committed_research_projection import runtime
from tests.test_research import fake_pypdf

URL = "https://example.com/versioned-article"


class ChangingSource:
    def __init__(self) -> None:
        self.body = "Original version.\n\n" + "Original paragraph about saffron, exactly 17 grams.\n\n" * 180
        self.calls = 0

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        self.calls += 1
        return {"status": "ok", "url": URL, "title": "Versioned article", "text": self.body}


def turn(session: TaskSession, tools: ResearchTools, managed: ManagedOutputStore, sink: KernelEffectSink,
         number: int, call: ToolCall) -> ToolResult:
    result = execute_turn(session, [call], run_id="r", effect_scope="task", turn=number,
        research_tools=tools, managed_outputs=managed, session_id="s", intent_sink=sink)[0]
    ProviderActions(SimpleNamespace(send=lambda _: "ack"), sink).send("results")
    return result


def test_located_v1_survives_explicit_v2_acquisition_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)
    backend = ChangingSource()
    tools.search = backend
    try:
        original = turn(session, tools, managed, sink, 1, ToolCall("open_url", {"url": URL}))
        assert original.ok
        located = turn(session, tools, managed, sink, 2, ToolCall("source_search", {"url": URL, "query": "saffron"}))
        assert located.ok
        old_target = dict(session.hit_targets["h1"])
        backend.body = "Updated version with no saffron; all offsets changed.\n" * 180
        updated = turn(session, tools, managed, sink, 3, ToolCall("open_url", {"url": URL}))
        assert updated.ok and backend.calls == 2
        opened = turn(session, tools, managed, sink, 4, ToolCall("open_hit", {"hit_id": "h1"}))
        assert opened.ok and "17 grams" in opened.model_text and backend.calls == 2
        assert session.hit_targets["h1"] == old_target
        assert opened.canonical["research_observation"] == original.canonical["research_observation"]

        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="chat")
        assert recovered.ok
        restored = TaskSession(session.policy)
        ledger = ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes)
        restore_task_session(frame, restored, research_ledger=ledger)
        assert restored.hit_targets == session.hit_targets
        assert captured_source(ledger, URL, old_target["text_hash"]) == original.canonical["research_observation"]
        assert backend.calls == 2
    finally:
        sink.durable.close()


@pytest.mark.parametrize("call", [ToolCall("reopen_source", {"source_id": "s1"}),
                                 ToolCall("open_url", {"url": URL, "offset": 6500})])
def test_reread_and_continuation_do_not_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call: ToolCall) -> None:
    session, tools, managed, _deps, sink = runtime(tmp_path, monkeypatch)
    backend = ChangingSource()
    tools.search = backend
    try:
        original = turn(session, tools, managed, sink, 1, ToolCall("open_url", {"url": URL}))
        backend.body = "Online V2 must not replace the captured body."
        reread = turn(session, tools, managed, sink, 2, call)
        assert reread.ok and "17 grams" in reread.model_text
        assert backend.calls == 1
        assert reread.canonical["research_observation"] == original.canonical["research_observation"]
    finally:
        sink.durable.close()


def test_missing_version_refuses_without_network() -> None:
    with pytest.raises(ValueError, match="captured source version is unavailable"):
        captured_source(ResearchLedger(), URL, "a" * 16)


def test_scanned_pdf_hit_retains_original_page_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, tools, managed, deps, sink = runtime(tmp_path, monkeypatch)

    class PdfSource(ChangingSource):
        def fetch(self, url: str) -> dict[str, object]:
            self.calls += 1
            return {"status": "ok", "url": URL, "title": "PDF", "content_kind": "pdf", "bytes": b"%PDF fixture"}

    backend = PdfSource()
    tools.search = backend
    try:
        with fake_pypdf(*(["Background."] * 8), "Background.\n\n" * 800 + "Saffron quantity is exactly 17 grams."):
            assert turn(session, tools, managed, sink, 1, ToolCall("open_url", {"url": URL})).ok
            located = turn(session, tools, managed, sink, 2, ToolCall("source_search", {"url": URL, "query": "saffron"}))
        assert located.ok and backend.calls == 2
        assert tools.ledger.pages_read_for_url(URL) == {1, 2, 3, 4, 5}
        assert not any(row.page == 9 for row in tools.ledger.read_passages.values())
        recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="chat")
        assert recovered.ok
        restored = TaskSession(session.policy)
        ledger = ResearchLedger()
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                                settled_tool_outcomes=recovered.settled_tool_outcomes)
        restore_task_session(frame, restored, research_ledger=ledger)
        tools.ledger = ledger
        # The extractor is no longer present. Only the committed scan can supply page 9.
        opened = turn(restored, tools, managed, sink, 3, ToolCall("open_hit", {"hit_id": "h1"}))
        assert opened.ok and "17 grams" in opened.model_text and backend.calls == 2
        assert any(row.page == 9 and "17 grams" in row.excerpt for row in tools.ledger.read_passages.values())
    finally:
        sink.durable.close()
