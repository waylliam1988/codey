"""Fewer navigation fields must preserve exact reads, holes, versions and recovery."""

import json
from copy import deepcopy

import pytest

from codey.research.answer_context import _render_context, answer_context
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
        installed = answer_context(session, tools.ledger, answer_scope(session))
        assert all(passage.excerpt in installed for passage in passages.values())
        if not offsets:
            assert "No original excerpt has been read for this task yet" in installed
            assert "Read a provided web URL with open_url" in installed
            assert "Previously read snapshots are indexed below" not in installed
        installed_packet = packet(installed)
        assert installed_packet["source_locators"] == packet(candidate)["source_locators"]
        assert installed_packet["previously_read_snapshots"] == []
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
def test_over_budget_originals_keep_stable_citations_without_receipt_recovery(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "Complete original material. " * 25 + "\n"
    try:
        for index in range(16):
            result = execute(delegate, tools, session, "open_url", {"url": URL + f"/{index}"})
            session._memory_results[f"receipt-{index}"] = result
        scope = answer_scope(session)
        cap = len(_render_context(session, tools.ledger, scope, scope.aliases[-2:]))
        text = answer_context(session, tools.ledger, scope, count_tokens=len, token_budget=cap)
        data = packet(text)
        assert len(text) <= cap and len(data["previously_read_snapshots"]) == 14
        assert "read_tool_result" not in text and "omitted_windows" not in data
        first = data["previously_read_snapshots"][0]
        assert first["label"] == "e1" and first["source_hash"]
        assert (first["start"], first["end"]) == (0, len(tools.search.text))
        assert "recover" not in first and "passage_ref" not in first
        assert len(scope.aliases) == 16
        submit_answer(scope, {"state": "answered", "markdown": "The earlier original material [e1]."})
        session._memory_results["recovered"] = ToolResult(ToolCall("read_tool_result", {"result_ref": "receipt-0"}),
                                                         "Legacy recovered original output", ok=True)
        assert answer_scope(session).aliases == scope.aliases
    finally:
        store.close()
