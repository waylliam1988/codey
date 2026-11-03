"""Exercise inventory findings through real scripted production research paths."""

import sys
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.research import pipeline
from tests.manual.research_process_review import ablation
from tests.test_research_deterministic_completion import _run
from tests.test_research_whole_kernel import run_whole


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("whole", [False, True])
def test_both_compilers_publish_without_importing_old_text_finalizer(tmp_path, monkeypatch, whole):
    monkeypatch.setitem(sys.modules, "codey.research.done_finalizer", None)
    result = run_whole(tmp_path, native=True) if whole else _run(tmp_path, native=True)
    assert result.stop_reason == "done", result.summary
    assert result.synthesis_id and result.completion_proof.satisfied


@pytest.mark.usefixtures("public_source_dns")
def test_domain_method_hints_are_bypassed_by_whole_answer_scope(tmp_path):
    from codey.research import methods

    with patch.object(methods, "research_method_guidance", side_effect=AssertionError("legacy method hints called")):
        result = run_whole(tmp_path, native=True)
    assert result.stop_reason == "done"


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("whole", [False, True])
def test_accepted_publication_reuses_the_audit(tmp_path, whole):
    original = pipeline.review_research_proof
    observations = []

    def review(*args, **kwargs):
        value = original(*args, **kwargs)
        observations.append((args, kwargs, value))
        return value

    with patch.object(pipeline, "review_research_proof", review):
        result = run_whole(tmp_path, native=True) if whole else _run(tmp_path, native=True)
    assert result.stop_reason == "done", result.summary
    assert len(observations) == 1


@pytest.mark.usefixtures("public_source_dns")
def test_removing_post_publication_review_loses_diagnostics_while_delivery_survives(tmp_path):
    class Trace:
        def __init__(self):
            self.rows = []

        def __getattr__(self, name):
            return lambda *args, **kwargs: self.rows.append((name, args, kwargs))

    before, after = Trace(), Trace()
    original_init = pipeline.ResearchPipeline.__init__

    def run(trace, directory, arm):
        def init(self, **kwargs):
            kwargs["context"] = replace(kwargs["context"], trace=trace)
            original_init(self, **kwargs)

        with patch.object(pipeline.ResearchPipeline, "__init__", init), ablation(arm):
            return run_whole(directory, native=True)

    baseline = run(before, tmp_path / "before", "baseline")
    candidate = run(after, tmp_path / "after", "no_proof_audit")
    assert baseline.summary == candidate.summary
    assert baseline.stop_reason == candidate.stop_reason == "done"
    assert any(name == "record_proof_review" and args[0] is not None for name, args, _ in before.rows)
    assert not any(name == "record_proof_review" and args[0] is not None for name, args, _ in after.rows)
    assert any(name == "record_research_brief_projection" for name, _, _ in before.rows)
    assert not any(name == "record_research_brief_projection" for name, _, _ in after.rows)


@pytest.mark.usefixtures("public_source_dns")
def test_web_receipt_requires_output_view_and_restores_the_original_alias(tmp_path, monkeypatch):
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
        budget = len(tools.search.text)
        assert first_alias not in answer_scope(session, budget=budget).aliases
        monkeypatch.setattr(tools.search, "fetch", lambda *a: pytest.fail("receipt restore refetched source"))

        wrong = read_tool_result(delegate, ToolCall("read_tool_result", {
            "result_ref": "original", "view": "value", "query": "Secret"}))
        assert not wrong.ok and "canonical value unavailable" in wrong.model_text
        assert first_alias not in answer_scope(session, budget=budget).aliases

        restored = read_tool_result(delegate, ToolCall("read_tool_result", {
            "result_ref": "original", "view": "output", "query": "Secret"}))
        assert restored.ok and "ZEPHYR-6842-Q" in restored.model_text
        session._memory_results["restored"] = restored
        assert first_alias in answer_scope(session, budget=budget).aliases
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
