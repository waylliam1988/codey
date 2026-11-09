"""The acquisition gate reads receipts and rejects misleading completion flags."""
from __future__ import annotations

import copy

import pytest

from codey.research.source_acquisition import attempt
from codey.types.json_value import JsonObject, JsonValue
from tools.web_source_acquisition_live_gate import PDF, RENDER, STATIC, analyze


@pytest.mark.parametrize("damage", ["none", "duplicate_http", "missing_pdf", "missing_locator", "no_progress"])
def test_gate_requires_real_acquisitions_and_complete_diagnostic_steps(damage: str) -> None:
    records: list[JsonObject] = []
    acquisitions: list[JsonObject] = []
    for url, grams in ((STATIC, "61"), (RENDER, "17"), (PDF, "37")):
        backends = ["http", "browser"] if url == RENDER else ["http"]
        entries: list[JsonValue] = [attempt(backend, url, {"status": "ok"}) for backend in backends]
        records.append({"args": {"url": url}, "canonical": {"source_acquisition": {"version": 1, "attempts": entries}},
                        "model_text": grams + " grams"})
        acquisitions.extend({"backend": backend, "url": url} for backend in backends)
    events: list[JsonObject] = [{"type": "tool", "tool_name": name, "ok": True}
                              for name in ("open_url", "source_search", "open_hit", "reopen_source")]
    if damage == "duplicate_http":
        acquisitions.append(copy.deepcopy(acquisitions[0]))
    elif damage == "missing_pdf":
        records.pop()
    elif damage == "missing_locator":
        events.pop()
    result = analyze(records, acquisitions, events, "no_progress" if damage == "no_progress" else "done")
    assert result["passed"] is (damage == "none")
