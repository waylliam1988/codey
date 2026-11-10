from __future__ import annotations

from itertools import permutations

import pytest

from codey.operations.reference_handles import ReferenceHandleError, ReferenceHandleResolver
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import source_observation, source_snapshot


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
