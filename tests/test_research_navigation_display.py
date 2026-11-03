"""Fewer navigation fields must preserve exact reads, holes, versions and recovery."""

import json
from copy import deepcopy

import pytest

from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope, submit_answer
from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_navigation_ab import compact_view
from tests.support.research_navigation_baseline import answer_context as baseline_context
from tests.test_research_findings import URL, execute, setup


def packet(text):
    return next(json.loads(block) for block in text.split("\n\n") if block.lstrip().startswith('{"investigation_attempts":'))


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("offsets", [[], [0], [0, 9000], [0, 1000]])
def test_candidate_changes_only_duplicate_fields_and_keeps_actual_first_gap(tmp_path, offsets):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "x" * (2000 if offsets == [0, 1000] else 12000)
    try:
        for index, offset in enumerate(offsets):
            result = execute(delegate, tools, session, "open_url", {"url": URL, "offset": offset, "limit": 1000})
            assert result.ok, result.model_text
            session._memory_results[f"read-{index}"] = result
        memory = deepcopy(session._memory_results)
        passages = deepcopy(tools.ledger.read_passages)
        before = baseline_context(session, tools.ledger, answer_scope(session))
        candidate = compact_view(before)
        assert answer_context(session, tools.ledger, answer_scope(session)) == candidate
        old, new = packet(before), packet(candidate)
        old.pop("read_receipts")
        for row in old["source_locators"]:
            row.pop("unread_intervals", None)
            row.pop("first_unread_interval", None)
        assert old == new
        assert before.replace(json.dumps(packet(before), ensure_ascii=False, sort_keys=True, separators=(",", ":")), "PACKET") == candidate.replace(
            json.dumps(new, ensure_ascii=False, sort_keys=True, separators=(",", ":")), "PACKET")
        assert session._memory_results == memory and tools.ledger.read_passages == passages
        if offsets:
            source = next(row for row in new["source_locators"] if "next_offset" in row)
            assert source["next_offset"] == (2000 if offsets == [0, 1000] else 1000)
            assert "sections" in source and source["source_hash"]
        assert len(candidate) <= len(before)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_all_hidden_original_labels_and_recovery_calls_survive_without_full_receipt_list(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "Complete original material. " * 25 + "\n"
    try:
        for index in range(16):
            result = execute(delegate, tools, session, "open_url", {"url": URL + f"/{index}"})
            assert result.ok
            session._memory_results[f"receipt-{index}"] = result
        scope = answer_scope(session, budget=1500)
        before = baseline_context(session, tools.ledger, scope)
        assert answer_context(session, tools.ledger, scope) == compact_view(before)
        old, new = packet(before), packet(compact_view(before))
        assert len(new["omitted_windows"]) == 14
        assert new["omitted_windows"] == old["omitted_windows"]
        for hidden in new["omitted_windows"]:
            identity = hidden["recover"]["args"]["result_ref"]
            assert hidden["passage_ref"] in {r["ref"] for r in session._memory_results[identity].canonical["read_passages"]}
        first = new["omitted_windows"][0]
        assert first["label"] == "e1" and "text" not in first
        with pytest.raises(ValueError, match="Unknown or unshown"):
            submit_answer(scope, {"state": "answered", "markdown": "The hidden original material [e1]."})
        session._memory_results["recovered"] = ToolResult(ToolCall("read_tool_result", first["recover"]["args"]),
                                                         "Recovered original output", ok=True)
        restored = answer_scope(session, budget=1500)
        assert ("e1", first["passage_ref"]) in restored.aliases
        submit_answer(restored, {"state": "answered", "markdown": "The original material [e1]."})
        assert len(compact_view(before)) < len(before)
    finally:
        store.close()
