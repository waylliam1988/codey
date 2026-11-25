"""Exercise inventory findings through real scripted production research paths."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
def test_whole_answer_publishes_without_the_retired_text_finalizer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "codey.research.done_finalizer", None)
    result = run_whole(tmp_path, native=True)
    assert result.stop_reason == "done", result.summary
    assert result.synthesis_id and result.completion_proof.satisfied


@pytest.mark.usefixtures("public_source_dns")
def test_domain_method_hints_are_bypassed_by_whole_answer_scope(tmp_path):
    from codey.research import methods

    with patch.object(methods, "research_method_guidance", side_effect=AssertionError("legacy method hints called")):
        result = run_whole(tmp_path, native=True)
    assert result.stop_reason == "done"


@pytest.mark.usefixtures("public_source_dns")
def test_receipt_output_search_preserves_task_read_authority_and_original_alias(tmp_path, monkeypatch):
    from codey.operations.tool_result_reader import read_tool_result
    from codey.research.answer_contract import answer_scope
    from codey.runtime.core.models import ToolCall
    from tests.test_research_findings import URL, execute, setup

    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "Original policy. " * 40 + "Secret is ZEPHYR-6842-Q."
    try:
        original = execute(delegate, tools, session, "open_url", {"url": URL})
        assert "value" not in original.canonical
        session._memory_results["original"] = original
        first_alias = answer_scope(session).aliases[0]
        noise = execute(delegate, tools, session, "open_url", {"url": URL + "/noise"})
        session._memory_results["noise"] = noise
        assert first_alias in answer_scope(session).aliases
        monkeypatch.setattr(tools.search, "fetch", lambda *a: pytest.fail("receipt restore refetched source"))

        wrong = read_tool_result(delegate, ToolCall("read_tool_result", {
            "result_ref": "original", "view": "value", "query": "Secret"}))
        assert not wrong.ok and "canonical value unavailable" in wrong.model_text
        assert first_alias in answer_scope(session).aliases

        restored = read_tool_result(delegate, ToolCall("read_tool_result", {
            "result_ref": "original", "view": "output", "query": "Secret"}))
        assert restored.ok and "ZEPHYR-6842-Q" in restored.model_text
        session._memory_results["restored"] = restored
        assert first_alias in answer_scope(session).aliases
        assert not store.search("Secret")
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
def test_whole_research_question_is_saved_without_publishing_an_answer(tmp_path, native):
    import json

    from codey.durable import DurableSession, SQLiteDurableStore

    def provider_wrap(provider):
        provider.send = lambda prompt: json.dumps({"tool": "ask_user", "args": {
            "question": "Which historical version should be inspected?", "options": ["Version one", "Version two"]}})
        return provider

    result = run_whole(tmp_path, native=native, provider_wrap=provider_wrap)
    assert result.stop_reason == "question", result.summary
    assert not result.synthesis_id and result.accepted_report is None
    durable = DurableSession(SQLiteDurableStore(tmp_path / "state.sqlite"))
    try:
        assert durable.task("r").state["question"]["id"]
    finally:
        durable.close()
