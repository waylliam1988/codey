"""Whole-task evidence selection stays frozen without pre-publication writes."""

import json
from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.research.answer_contract import AnswerScope, answer_scope, submit_answer
from codey.research.answer_report import AnswerReport, compile_answer_report
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_research_findings import URL, execute, setup


def record_submission(session, args):
    submission = submit_answer(answer_scope(session), args)
    session._memory_results["submitted"] = ToolResult(ToolCall("submit_research_answer", args), "Recorded", ok=True,
        canonical={"research_submission": submission})
    return submission


@pytest.mark.usefixtures("public_source_dns")
def test_cross_source_submission_prepares_one_atomic_archive_without_claims(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        tools.search.text = "The second source describes independent limits and exceptions."
        session._memory_results["read2"] = execute(delegate, tools, session, "open_url", {"url": URL + "-second"})
        scope = answer_scope(session)
        assert tuple(label for label, _ in scope.aliases) == ("e1", "e2")
        assert AnswerScope.from_payload(scope.to_payload()) == scope
        record_submission(session, {"state": "partial", "markdown": "Concurrent reads [e1]. Independent limits [e2].\nStill unknown: performance."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert not store.repository.read(report.knowledge_refs[0]["id"])
        assert len(report.source_versions) == 2
        assert {row["value"]["kind"] for row in report.knowledge_mutations} == {"source", "evidence"}
        assert AnswerReport.from_payload(report.to_payload()) == report
        mutations = [KnowledgeMutation(KnowledgeObject.from_dict(row["value"]), row["expected_revision"])
                     for row in report.knowledge_mutations]
        answer = KnowledgeObject("answer-test", "answer", report.text, "model_inference", scope="session", scope_id="first",
            references=tuple(KnowledgeReference(row["id"], row["revision"], "uses") for row in report.knowledge_refs))
        mutations.append(KnowledgeMutation(answer))
        first = store.commit_knowledge("publication-test", mutations)
        assert store.commit_knowledge("publication-test", mutations) == first
        assert store.repository.read("answer-test").interpretation_review == "unreviewed"
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_same_url_changes_never_rebind_aliases_or_selected_original_text(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        original = answer_scope(session)
        old_text = next(iter(tools.ledger.read_passages.values())).excerpt
        tools.search.text = "Changed source now makes a different statement."
        session._memory_results["read2"] = execute(delegate, tools, session, "open_url", {"url": URL})
        current = answer_scope(session)
        assert current.aliases[0] == original.aliases[0] and current.aliases[1][0] == "e2"
        record_submission(session, {"state": "answered", "markdown": "The original source described concurrency [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert report.evidence_bindings[0]["excerpt"] == old_text
        assert report.evidence_bindings[0]["passage_ref"] == original.passage_refs[0]
        assert len(report.source_versions) == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_omitted_window_is_not_citable_and_recovery_keeps_original_alias(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read1"] = execute(delegate, tools, session, "open_url", {"url": URL})
        original = answer_scope(session)
        omitted = answer_scope(session, budget=1)
        assert not omitted.aliases
        with pytest.raises(ValueError, match="unshown"):
            submit_answer(omitted, {"state": "answered", "markdown": "Concurrency is supported [e1]."})
        assert answer_scope(session).aliases == original.aliases
        frozen = AnswerScope.from_payload(json.loads(json.dumps(original.to_payload())))
        assert submit_answer(frozen, {"state": "answered", "markdown": "Concurrency is supported [e1]."})["citations"]
    finally:
        store.close()


def test_failed_investigation_can_publish_gap_without_fabricated_source(tmp_path):
    store, tools, session, _ = setup(tmp_path)
    try:
        session._memory_results["failed"] = ToolResult(ToolCall("open_url", {"url": URL}), "Timeout", ok=False)
        record_submission(session, {"state": "unresolved", "markdown": "The source timed out. This investigation cannot determine the result."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        assert not report.source_versions and not report.knowledge_mutations
        assert report.state == "unresolved" and report.attempt_refs == ("failed",)
        assert report.open_questions == (session.task_text,)
    finally:
        store.close()


@pytest.mark.parametrize("markdown", ["Only a code example: `[e1]`.", "Only a fenced example:\n```text\n[e1]\n```"])
def test_code_examples_do_not_count_as_adopted_evidence(markdown):
    scope = AnswerScope("a" * 64, (("e1", "p" + "b" * 12),), ("read",))
    with pytest.raises(ValueError, match="needs explicit"):
        submit_answer(scope, {"state": "answered", "markdown": markdown})


@pytest.mark.parametrize("change", ["task", "citation", "text", "claim"])
@pytest.mark.usefixtures("public_source_dns")
def test_changed_frozen_report_is_rejected(tmp_path, change):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        record_submission(session, {"state": "answered", "markdown": "Readers and writers can overlap [e1]."})
        report = compile_answer_report(session, tools.ledger, store, project="", session_id="first")
        if change == "task":
            report = replace(report, questions=("Another task",))
        elif change == "citation":
            report = replace(report, citation_bindings=())
        elif change == "text":
            report = replace(report, text="A different answer")
        else:
            report = replace(report, knowledge_mutations=({"value": KnowledgeObject("invented", "claim", "Claim", "model_extraction").to_dict(),
                                                          "expected_revision": 0},))
        with pytest.raises(ValueError):
            AnswerReport.from_payload(report.to_payload())
    finally:
        store.close()
