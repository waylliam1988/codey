from __future__ import annotations

from itertools import permutations

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import source_observation, source_snapshot
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject
from tests.test_committed_research_projection import runtime


class _ChangingSource:
    def __init__(self) -> None:
        self.body = "Initial article amount is 17 grams.\n" * 32

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        return []

    def fetch(self, url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "title": "Record", "text": self.body}


@pytest.mark.parametrize("clock", ["2026-11-10T00:00:00+00:00", "2020-01-01T00:00:00+00:00"])
def test_sequential_captures_keep_original_order_under_equal_or_backwards_clock(monkeypatch, clock: str) -> None:
    url = "https://example.com/record"
    ledger = ResearchLedger()
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})))
    session.source_ids["s1"] = url
    resolver = ReferenceHandleResolver(session, ledger=ledger)
    monkeypatch.setattr("codey.research.ledger.now_iso", lambda: "2026-11-10T00:00:00+00:00")
    ledger.record_open(url, url, "Record", "an exact passage")
    handle = resolver.handle_for("s1")
    observations = [source_snapshot(ledger.opened_sources[0], ledger.source_text_for_url(url), {})]
    monkeypatch.setattr("codey.research.ledger.now_iso", lambda: clock)
    for text in ("a new passage", "the final passage"):
        ledger.record_open(url, url, "Record", text)
        observations.append(source_snapshot(ledger.opened_sources[0], text, {}))
    with pytest.raises(ReferenceHandleError, match="different source version"):
        resolver.resolve(handle)
    assert ledger.source_text_for_url(url) == "the final passage"
    # Reconstruct only from original receipts, including duplicate replay.
    for ordered in permutations(observations):
        restored = ResearchLedger()
        for receipt in (*ordered, *ordered):
            source, text, pages = source_observation(receipt)
            restored.merge_opened_source(source, text, pages)
        assert restored.opened_sources == ledger.opened_sources
        assert restored.source_text_for_url(url) == "the final passage"


def test_production_delegate_stamps_each_read_from_formal_source_versions(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, tools, managed, _deps, _sink = runtime(tmp_path, monkeypatch)
    backend = _ChangingSource()
    tools.search = backend
    url = "https://example.com/record"
    first_stamp = "2026-11-10T00:00:00+00:00"
    monkeypatch.setattr("codey.research.ledger.now_iso", lambda: first_stamp)
    first = execute_turn(session, [ToolCall("open_url", {"url": url})], run_id="r", effect_scope="task",
                         turn=1, research_tools=tools, managed_outputs=managed, session_id="s")[0]
    assert first.ok

    backend.body = "New amount is 0 grams.\n" * 32
    monkeypatch.setattr("codey.research.ledger.now_iso", lambda: "2020-01-01T00:00:00+00:00")
    second = execute_turn(session, [ToolCall("open_url", {"url": url})], run_id="r", effect_scope="task",
                          turn=2, research_tools=tools, managed_outputs=managed, session_id="s")[0]
    assert second.ok

    first_source, _, _ = source_observation(first.canonical["research_observation"])
    second_source, _, _ = source_observation(second.canonical["research_observation"])
    assert second_source.retrieved_at > first_source.retrieved_at
    assert "New amount is 0 grams." in tools.ledger.source_text_for_url(url)
