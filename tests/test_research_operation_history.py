"""Successful read deduplication must leave exact recovery and failures intact."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from codey.runtime.core.models import ToolCall, ToolResult
from tests.manual.research_operation_history_ab import original_history, receipt_only_history, treatment
from tests.test_research_findings import URL, execute, setup


def result(name="knowledge_read", *, ok=True, args=None, passages=True, text="observed"):
    return ToolResult(ToolCall(name, args or {"id": "source-exact", "revision": 1}), text, ok=ok,
                      canonical={"read_passages": [{"ref": "p-exact"}]} if passages else {})


def test_successful_search_and_prior_claim_are_not_discarded():
    session = SimpleNamespace(_memory_results={
        "s": result("knowledge_search", passages=False),
        "claim": result(passages=False, args={"id": "claim-exact", "revision": 1}), "read": result(),
    })
    before = deepcopy(session._memory_results)
    rows = receipt_only_history(session, original_history(session))
    assert [row["result_ref"] for row in rows] == ["s", "claim"]
    assert session._memory_results == before


@pytest.mark.parametrize("special", [
    result(ok=False, passages=False, text="ERROR exact-id is unavailable; revision=7"),
    result("submit_research_answer", ok=False, text="Unshown citation e99"),
    result("read_tool_result"), result("prepare_knowledge_revision", passages=False),
    result("knowledge_commit", passages=False), result("knowledge_write", passages=False),
])
def test_old_errors_edits_and_recovery_keep_the_original_presentation(special):
    session = SimpleNamespace(_memory_results={"old-special": special})
    for i in range(12):
        session._memory_results[f"r{i}"] = result(args={"id": f"source-{i}", "revision": 1})
    before = original_history(session)
    assert receipt_only_history(session, before) == before
    assert len(before) == 8


def test_repeated_observations_before_visible_tail_disable_compaction():
    session = SimpleNamespace(_memory_results={"repeat1": result(), "repeat2": result()})
    for i in range(12):
        session._memory_results[f"r{i}"] = result(args={"id": f"source-{i}", "revision": 1})
    before = original_history(session)
    assert receipt_only_history(session, before) == before


@pytest.mark.parametrize("tail", [None, result(ok=False), result("read_tool_result"),
                                  result("knowledge_commit"), result(),
                                  result("knowledge_search", passages=False)])
def test_installed_projection_matches_the_isolated_candidate(tail):
    from codey.research.answer_context import investigation_history

    session = SimpleNamespace(_memory_results={"read": result(),
        "search": result("knowledge_search", passages=False)})
    if tail is not None:
        session._memory_results["tail"] = tail
    expected = original_history(session)
    if tail is not None and not tail.ok:
        assert expected[-1].pop("error") == tail.model_text
    assert investigation_history(session) == expected
    empty = result("knowledge_search", passages=False, args={"query": "missing"})
    empty = ToolResult(empty.call, empty.model_text, ok=True, canonical={"knowledge_search_empty": True})
    session._memory_results["empty"] = empty
    assert investigation_history(session) == receipt_only_history(session, original_history(session))


@pytest.mark.usefixtures("public_source_dns")
def test_actual_work_view_changes_only_duplicate_reads_and_restores_them_on_failure(tmp_path):
    from codey.research.answer_context import answer_context
    from codey.research.answer_contract import answer_scope

    store, tools, session, delegate = setup(tmp_path)

    def view(arm):
        with treatment(arm):
            text = answer_context(session, tools.ledger, answer_scope(session))
        blocks = text.split("\n\n")
        index = next(i for i, block in enumerate(blocks) if block.startswith('{"investigation_attempts":'))
        data = json.loads(blocks[index])
        blocks[index] = "<same work packet>"
        return blocks, data

    try:
        tools.search.text = "Complete original material. " * 25
        for i in range(16):
            observed = execute(delegate, tools, session, "open_url", {"url": URL + f"/{i}"})
            session._memory_results[f"receipt-{i}"] = observed
        memory = dict(session._memory_results)
        assert view("production") == view("original_control")
        searched = execute(delegate, tools, session, "knowledge_search", {"query": "missing-topic-9a27"})
        assert searched.ok and searched.canonical.get("knowledge_search_empty") is True
        session._memory_results["empty-search"] = searched
        memory["empty-search"] = searched
        before_blocks, before = view("original_control")
        after_blocks, after = view("production")
        assert before_blocks == after_blocks
        assert len(before.pop("investigation_attempts")) == 8
        assert [row["result_ref"] for row in after.pop("investigation_attempts")] == ["empty-search"]
        assert before == after
        assert "read_receipts" not in after and after["previously_read_snapshots"] == []
        assert session._memory_results == memory
        session._memory_results["failure"] = result(ok=False, passages=False, text="Bad exact-id revision=7")
        before_blocks, before = view("original_control")
        after_blocks, after = view("production")
        assert before_blocks == after_blocks
        assert before["investigation_attempts"][-1].pop("error") == "Bad exact-id revision=7"
        assert before == after and after["latest_error"] == ["Bad exact-id revision=7"]
        session._memory_results["later-success"] = result(passages=False, args={"id": "correct-id", "revision": 7})
        assert view("original_control") == view("production")
        session._memory_results["later-empty"] = searched
        assert view("original_control") == view("production")
    finally:
        store.close()


def test_empty_search_marker_requires_no_typed_or_legacy_hits(tmp_path, monkeypatch):
    store, tools, session, delegate = setup(tmp_path)
    try:
        empty = execute(delegate, tools, session, "knowledge_search", {"query": "missing"})
        assert empty.ok and empty.canonical == {"knowledge_search_empty": True}
        monkeypatch.setattr(store, "search_notes", lambda *a, **kw: [{"id": "legacy", "type": "fact", "title": "Lead"}])
        legacy = execute(delegate, tools, session, "knowledge_search", {"query": "missing"})
        assert legacy.ok and not legacy.canonical
        monkeypatch.setattr(store, "search_notes", lambda *a, **kw: [])
        monkeypatch.setattr(store, "search", lambda *a, **kw: [object()])
        monkeypatch.setattr("codey.research.tools.render_recall", lambda *a, **kw: "A typed knowledge lead")
        typed = execute(delegate, tools, session, "knowledge_search", {"query": "missing"})
        assert typed.ok and not typed.canonical
        invalid = execute(delegate, tools, session, "knowledge_search", {"query": ""})
        assert not invalid.ok and not invalid.canonical
    finally:
        store.close()


def test_old_or_text_only_search_receipt_keeps_original_history():
    from codey.research.answer_context import investigation_history

    session = SimpleNamespace(_memory_results={"read": result(), "search": result(
        "knowledge_search", passages=False, text="no local notes yet on this topic")})
    assert investigation_history(session) == original_history(session)
