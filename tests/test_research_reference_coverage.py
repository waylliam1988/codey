"""One reference namespace, explicit question gaps, and replayable plan receipts."""

import json

import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.operations.kernel_facts import project_result_observation
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.ledger import ResearchLedger
from codey.research.questions import question_plan, validate_question_coverage
from tests.test_research_findings import URL, execute, setup


def test_question_identity_is_append_only_and_plan_receipt_restores_both_projections(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.ledger.questions = session.research_questions = ()
        result = execute(delegate, tools, session, "research_plan", {"questions": ["Concurrency?", "Limits?"]})
        assert result.ok
        restored = ResearchLedger()
        recovered_session = TaskSession(session.policy)
        project_result_observation(recovered_session, result.call, result, ok=True, research_ledger=restored)
        assert recovered_session.research_questions == restored.questions == ("Concurrency?", "Limits?")
        assert question_plan(["Concurrency?", "Limits?", "Versions?"], restored.questions)[-1] == "Versions?"
        for proposed in (["Limits?", "Concurrency?"], ["Concurrency?"], ["Changed?", "Limits?"]):
            with pytest.raises(ValueError, match="Keep existing"):
                question_plan(proposed, restored.questions)
    finally:
        store.close()


def test_each_gap_requires_its_own_selected_real_investigation_record():
    ledger = ResearchLedger()
    ledger.questions = ("Writer concurrency?", "Checkpoint limits?")
    first = {"question_ref": "q1", "state": "not_investigated", "answer": "Not yet investigated",
             "findings": [], "investigations": []}
    second = {**first, "question_ref": "q2"}
    assert len(validate_question_coverage([first, second], ledger)) == 2
    with pytest.raises(ValueError, match="Missing: q2"):
        validate_question_coverage([first], ledger)
    with pytest.raises(ValueError, match="distinct q1"):
        validate_question_coverage([first, first], ledger)
    searched = {**second, "state": "unanswered_after_search", "investigations": ["a1"]}
    with pytest.raises(ValueError, match="unknown investigation a1"):
        validate_question_coverage([first, searched], ledger)
    ledger.investigations["a1"] = {"tool": "web_search", "question_ref": "q2", "ok": False, "args": {"query": "checkpoint limits"}}
    assert validate_question_coverage([first, searched], ledger)[1]["investigations"] == ["a1"]
    # Existence is mechanical; semantic relevance of a1 to q2 is not certified.
    with pytest.raises(ValueError, match="actual investigations"):
        validate_question_coverage([first, {**searched, "investigations": []}], ledger)
    with pytest.raises(ValueError, match="unknown finding"):
        validate_question_coverage([{**first, "state": "partial", "findings": ["f99"]}, second], ledger)


def test_old_answers_private_and_truncated_objects_cannot_be_promoted_to_reference(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        store.commit_knowledge("seed", [KnowledgeMutation(KnowledgeObject("old-answer", "answer", "An old answer", "model_inference")),
            KnowledgeMutation(KnowledgeObject("private", "claim", "Private personal information", "user_statement",
                                               scope="personal", scope_id="someone-else")),
            KnowledgeMutation(KnowledgeObject("too-long", "claim", "X" * 12001, "model_extraction"))])
        assert execute(delegate, tools, session, "knowledge_read", {"id": "old-answer"}).ok
        assert not execute(delegate, tools, session, "knowledge_read", {"id": "private"}).ok
        assert execute(delegate, tools, session, "knowledge_read", {"id": "too-long"}).ok
        assert not tools.ledger.references
        result = execute(delegate, tools, session, "save_inference", {
            "statement": "A hypothesis based on an old answer", "premise_refs": ["old-answer"], "limitations": "Unknown"})
        assert not result.ok
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_staged_finding_retry_keeps_same_identity_before_commit(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        staged = tools.create_staged()
        args = {"claim": "Only one writer can write at a time.", "source_ref": URL,
                "passage_ref": next(iter(staged.ledger.read_passages))}
        first = staged.save_finding(args)
        second = staged.save_finding(args)
        assert first.ok and second.ok, second.model_text
        assert json.loads(first.model_text)["finding_ref"] == json.loads(second.model_text)["finding_ref"]
        tools.commit_staged(staged)
        assert len(tools.ledger.references) == 1
    finally:
        store.close()


@pytest.mark.parametrize("inference", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_shared_driver_reuses_prior_knowledge_and_can_save_a_candidate_link(tmp_path, inference, monkeypatch):
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS
    from tests.support.kernel_runtime import run_task_kernel

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        result = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(result.model_text)["finding_ref"]
        claim_id = stable_identity("claim", tools.ledger.references[label].note_id, "Only one writer can write at a time.")
        material = store.repository.read(claim_id).value.references[0]
        tools.ledger = ResearchLedger()
        tools.session_id = "later-session"
        session = TaskSession(TaskPolicy(session.policy.grants, strict_research=True,
            required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS), task_kind="research", max_turns=7,
            task_text="Use prior research without fetching webpages again")
        hypothesis = "A single application writer queue may reduce lock contention; workload validation is needed."

        class Provider:
            turn = 0

            def send(self, prompt):
                self.turn += 1
                if self.turn == 1:
                    name, args = "knowledge_read", {"id": material.target, "revision": material.revision}
                else:
                    name, args = "submit_research_answer", {"state": "answered",
                        "markdown": "Only one writer can write at a time [e1]."}
                    if inference:
                        args["markdown"] += " Candidate deduction: " + hypothesis + " [e1]."
                        args["knowledge_updates"] = [{"key": "hypothesis", "kind": "inference",
                            "content": hypothesis, "references": ["e1"]}]
                return json.dumps({"tool": name, "args": args})

        monkeypatch.setattr(tools.search, "fetch", lambda *_: pytest.fail("prior source was fetched again"))
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(Provider(), user_task=session.task_text),
            execution=KernelExecutionDeps(research_tools=tools, permission_profile="research"),
            observation=KernelObservationDeps(completion_context={"research_ledger": tools.ledger})))
        assert result.completed, result.summary
        assert tools.ledger.opened_sources and not tools.ledger.evidence_items
        binding, = result.research_report.to_payload()["evidence_bindings"]
        assert binding["evidence_id"] == material.target
        assert binding["evidence_revision"] == material.revision
        if inference:
            assert "f2" not in result.summary
            assert result.research_report.to_payload()["knowledge_batch"]["updates"][0]["kind"] == "inference"
            assert hypothesis in result.summary
            assert "Only one writer can write at a time [e1]." in result.summary
    finally:
        store.close()
