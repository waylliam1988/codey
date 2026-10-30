"""One reference namespace, explicit question gaps, and replayable plan receipts."""

import copy
import json

import jsonschema
import pytest

from codey.knowledge.archive import stable_identity
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.finding_report import compile_finding_report
from codey.research.ledger import ResearchLedger
from codey.research.questions import question_plan, validate_question_coverage
from codey.research.tool_contract import STRUCTURED_REPORT_EXAMPLE, openai_parameters_schema, validate_tool_args
from codey.reviews.research_coverage import coverage_rows
from tests.test_research_findings import URL, execute, report_args, setup


def test_actual_report_example_is_accepted_by_schema_parser_and_coverage():
    example = json.loads(STRUCTURED_REPORT_EXAMPLE)
    jsonschema.validate(example["args"], openai_parameters_schema("done"))
    assert validate_tool_args("done", example["args"]).ok
    coverage_rows(example["args"]["report_sections"]["coverage"])
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"}), strict_research=True))
    snapshot = build_turn_snapshot(session)
    done = next(spec for spec in snapshot.frozen_specs if spec.name == "done")
    assert done.json_examples == (STRUCTURED_REPORT_EXAMPLE,)
    assert not normalize_turn(decode_text_turn(STRUCTURED_REPORT_EXAMPLE), snapshot=snapshot).protocol_error


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
    ledger.investigations["a1"] = {"tool": "web_search", "ok": False, "args": {"query": "checkpoint limits"}}
    assert validate_question_coverage([first, searched], ledger)[1]["investigations"] == ["a1"]
    # Existence is mechanical; semantic relevance of a1 to q2 is not certified.
    with pytest.raises(ValueError, match="actual investigations"):
        validate_question_coverage([first, {**searched, "investigations": []}], ledger)
    with pytest.raises(ValueError, match="unknown finding"):
        validate_question_coverage([{**first, "state": "partial", "findings": ["f99"]}, second], ledger)


def test_missing_evidence_is_a_research_completion_defect_not_a_json_repair():
    args = json.loads(STRUCTURED_REPORT_EXAMPLE)["args"]
    args["report_sections"]["coverage"][0]["findings"] = []
    # The call is structurally valid. Resending the same prose is not the remedy.
    assert validate_tool_args("done", args).ok
    state = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"}), strict_research=True))
    plan = normalize_turn(decode_text_turn(json.dumps({"tool": "done", "args": args})),
                          snapshot=build_turn_snapshot(state))
    assert not plan.protocol_error and plan.control is not None
    ledger = ResearchLedger()
    ledger.questions = ("What must be answered?",)
    with pytest.raises(ValueError, match="Changing evidence_found to partial alone") as failure:
        validate_question_coverage(args["report_sections"]["coverage"], ledger)
    assert "q1" in str(failure.value) and "Next action: investigate" in str(failure.value)
    assert "not_investigated" in str(failure.value) and "unanswered_after_search" in str(failure.value)
    # An honestly stated gap is admissible; it is not certified as an answer.
    args["report_sections"]["coverage"][0].update(
        state="not_investigated", answer="No direct investigation of this question yet.")
    assert validate_question_coverage(args["report_sections"]["coverage"], ledger)


@pytest.mark.usefixtures("public_source_dns")
def test_recalled_claim_pins_old_version_without_new_fetch_or_duplicate_archive(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        claim_id = stable_identity("claim", tools.ledger.references[label].note_id, "Only one writer can write at a time.")
        # A new invocation has no opened webpages or newly saved findings.
        tools.ledger = ResearchLedger()
        tools.ledger.questions = ("What limits writer concurrency?",)
        tools.session_id = "second"
        session = TaskSession(session.policy)
        delegate = ExecutionDelegate(session=session, research_tools=tools)
        read = execute(delegate, tools, session, "knowledge_read", {"id": claim_id, "revision": 1})
        assert read.ok
        adopted = json.loads(read.model_text)["finding_ref"]
        assert adopted == "f1"
        assert not tools.ledger.opened_sources and not tools.ledger.evidence_items
        compiled = compile_finding_report(report_args(adopted), tools.ledger, store, session_id="second")
        assert compiled.knowledge_refs == ({"id": claim_id, "revision": 1},)
        assert "本轮未重新获取" in compiled.text
        assert compiled.reference_metadata[adopted]["interpretation_review"] == "unreviewed"
        assert not tools.ledger.opened_sources
        from codey.operations.completion_gate import evaluate
        from codey.policies.task_policy import STRICT_RESEARCH_REQUIRED_CHECKS

        check_session = TaskSession(TaskPolicy(session.policy.grants, strict_research=True,
            required_checks=STRICT_RESEARCH_REQUIRED_CHECKS), task_kind="research")
        verdict = evaluate(check_session, compiled.text, context={"research_ledger": tools.ledger, "research_report": compiled})
        assert verdict.complete, verdict.followup
        restored = ResearchLedger()
        project_result_observation(TaskSession(session.policy), read.call, read, ok=True, research_ledger=restored)
        assert restored.references == tools.ledger.references
        # A later update must not silently change the already bound reference.
        old = store.repository.read(claim_id)
        from dataclasses import replace
        store.commit_knowledge("revise-claim", [KnowledgeMutation(replace(old.value, content="Updated statement"), 1)])
        again = compile_finding_report(report_args(adopted), tools.ledger, store, session_id="second")
        assert again.knowledge_refs[0]["revision"] == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_inference_pins_premises_keeps_candidate_identity_and_replays(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        first = json.loads(saved.model_text)["finding_ref"]
        args = {"statement": "Serializing application writes may reduce writer contention; this needs a workload test.",
                "premise_refs": [first], "limitations": "No direct experiment with the application workload."}
        saved_inference = execute(delegate, tools, session, "save_inference", args)
        assert saved_inference.ok, saved_inference.model_text
        second = json.loads(saved_inference.model_text)["finding_ref"]
        assert (first, second) == ("f1", "f2")
        binding = tools.ledger.references[second]
        version = store.repository.read(binding.object_id, binding.revision)
        assert version.value.kind == "inference" and version.value.status == "candidate"
        assert version.interpretation_review == "unreviewed"
        assert all(ref.relation == "depends_on" for ref in version.value.references)
        replay = execute(delegate, tools, session, "save_inference", args)
        assert replay.ok and json.loads(replay.model_text)["finding_ref"] == second
        assert store.repository.read(binding.object_id).revision == 1
        report = report_args(second)
        report["summary"] = args["statement"] + " [f2]"
        report["report_sections"]["evidence"] = ["Direct premise: Only one writer can write at a time. [f1]",
                                                  args["statement"] + " [f2]"]
        compiled = compile_finding_report(report, tools.ledger, store, session_id=tools.session_id)
        assert "候选推论 f2，未审查" in compiled.text
        assert compiled.reference_metadata[second]["kind"] == "inference"
        assert not any(item.claim == args["statement"] for item in tools.ledger.evidence_items)
        from codey.operations.completion_gate import evaluate
        from codey.policies.task_policy import STRICT_RESEARCH_REQUIRED_CHECKS

        check_session = TaskSession(TaskPolicy(session.policy.grants, strict_research=True,
            required_checks=STRICT_RESEARCH_REQUIRED_CHECKS), task_kind="research")
        verdict = evaluate(check_session, compiled.text, context={"research_ledger": tools.ledger, "research_report": compiled})
        assert verdict.complete, verdict.followup
        forged = copy.deepcopy(saved_inference)
        forged.canonical["research_reference_bindings"]["f1"] = binding.to_dict()
        with pytest.raises(ValueError, match="conflicting"):
            project_result_observation(session, forged.call, forged, ok=True, research_ledger=tools.ledger)
    finally:
        store.close()


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
def test_shared_driver_reuses_prior_knowledge_and_can_save_a_candidate_link(tmp_path, inference):
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.policies.task_policy import STRICT_RESEARCH_REQUIRED_CHECKS
    from tests.support.kernel_runtime import run_task_kernel

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        result = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(result.model_text)["finding_ref"]
        claim_id = stable_identity("claim", tools.ledger.references[label].note_id, "Only one writer can write at a time.")
        tools.ledger = ResearchLedger()
        tools.session_id = "later-session"
        session = TaskSession(TaskPolicy(session.policy.grants, strict_research=True,
            required_checks=STRICT_RESEARCH_REQUIRED_CHECKS), task_kind="research", max_turns=7)
        hypothesis = "A single application writer queue may reduce lock contention; workload validation is needed."

        class Provider:
            turn = 0

            def send(self, prompt):
                self.turn += 1
                if self.turn == 1:
                    name, args = "research_plan", {"questions": ["What limits writer concurrency?"]}
                elif self.turn == 2:
                    name, args = "knowledge_read", {"id": claim_id, "revision": 1}
                elif inference and self.turn == 3:
                    name, args = "save_inference", {"statement": hypothesis, "premise_refs": ["f1"],
                        "limitations": "No direct workload test; this is only a candidate connection."}
                else:
                    name, args = "done", report_args("f1")
                    if inference:
                        args["summary"] = hypothesis + " [f2]"
                        args["report_sections"]["evidence"].append(hypothesis + " [f2]")
                return json.dumps({"tool": name, "args": args})

        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(Provider(), user_task="Use prior research, without fetching webpages again"),
            execution=KernelExecutionDeps(research_tools=tools, permission_profile="research"),
            observation=KernelObservationDeps(completion_context={"research_ledger": tools.ledger})))
        assert result.completed, result.summary
        assert not tools.ledger.opened_sources and not tools.ledger.evidence_items
        assert "本轮未重新获取" in result.summary
        if inference:
            assert "候选推论 f2，未审查" in result.summary
    finally:
        store.close()
