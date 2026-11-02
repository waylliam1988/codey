"""One gate compilation owns diagnostics, proof references and accepted replay."""

import copy
import json
from dataclasses import replace

import pytest

from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations import completion_gate
from codey.operations.kernel_context import KernelContext
from codey.operations.kernel_contracts import KernelExecutionDeps
from codey.operations.research_continuation import ResearchContinuation
from codey.operations.research_publication import publish_synthesis
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import STRICT_RESEARCH_REQUIRED_CHECKS, TaskPolicy
from codey.research.finding_report import FindingReport
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime
from tests.support.kernel_restart import SimulatedExit
from tests.test_research_findings import URL, execute, report_args, selection_args, setup
from tests.test_research_gap_compilation import prepare

pytestmark = pytest.mark.usefixtures("public_source_dns")


def strict_session(session):
    return TaskSession(TaskPolicy(session.policy.grants, strict_research=True,
        required_checks=STRICT_RESEARCH_REQUIRED_CHECKS), task_kind="research", task_text=session.task_text,
        research_questions=session.research_questions)


def test_gate_keeps_field_diagnostics_and_prepares_before_citations(tmp_path, monkeypatch):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "passage_ref": next(iter(tools.ledger.read_passages))})
        session = strict_session(session)
        session.last_done_args = report_args("f1")
        session.last_done_args["summary"] = "A claim missing its citation."
        original = copy.deepcopy(session.last_done_args)
        context = {"research_ledger": tools.ledger, "knowledge_store": store,
                   "knowledge_session_id": tools.session_id}
        calls = []
        citations = completion_gate.citation_requirement_checks

        def checked(session, text, context, *, report=None):
            assert report is not None and text == report.text
            calls.append(report)
            return citations(session, text, context, report=report)

        monkeypatch.setattr(completion_gate, "citation_requirement_checks", checked)
        rejected = completion_gate.evaluate(session, "original draft", context=context)
        assert not rejected.complete and rejected.research_report is None
        assert "summary" in rejected.followup
        assert "[f1]" in rejected.followup and "does not require a new search or write" in rejected.followup
        assert session.last_done_args == original and calls == []
        session.last_done_args = selection_args("f1")
        original = copy.deepcopy(session.last_done_args)
        accepted = completion_gate.evaluate(session, "original draft", context=context)
        assert accepted.complete, accepted.followup
        assert calls == [accepted.research_report]
        assert accepted.final_text == accepted.research_report.text
        assert any(ref.startswith("knowledge-source:") for ref in accepted.proof.evidence_refs)
        assert session.last_done_args == original and session.last_done_text == ""
        assert "research_report" not in context
    finally:
        store.close()


@pytest.mark.parametrize("gap_state", ["not_investigated", "unanswered_after_search"])
def test_q8_gate_snapshot_publication_and_restart_share_one_report(tmp_path, monkeypatch, gap_state):
    store, tools, original_session, args = prepare(tmp_path / "knowledge", gap_state)
    durable, mutations, outputs, sink = runtime(tmp_path / "runtime", sqlite=True, task_kind="research")
    try:
        session = strict_session(original_session)
        session.last_done_args = copy.deepcopy(args)
        unsafe = args["report_sections"]["coverage"][-1]["answer"]
        original = copy.deepcopy(args)
        direct = completion_gate.evaluate(session, "", context={"research_ledger": tools.ledger,
            "knowledge_store": store, "knowledge_session_id": tools.session_id})
        assert direct.complete, direct.followup
        assert unsafe not in direct.final_text and session.last_done_args == original
        # Current wire proposals select findings; historical free answer text
        # remains covered by the gate test above.
        selection = {"report_sections": {"coverage": [
            {key: value for key, value in row.items() if key != "answer"}
            for row in args["report_sections"]["coverage"]]}}
        session.last_done_args = copy.deepcopy(selection)
        selected = completion_gate.evaluate(session, "", context={"research_ledger": tools.ledger,
            "knowledge_store": store, "knowledge_session_id": tools.session_id})
        assert selected.complete, selected.followup
        sends = []

        class Provider:
            def send(self, text):
                sends.append(text)
                return json.dumps({"tool": "done", "args": selection})

        request = request_for(durable, sink, Provider())
        request = replace(request, transport=replace(request.transport, user_task=session.task_text),
                          execution=KernelExecutionDeps(research_tools=tools))
        armed = [True]

        def committed(*_args):
            current = operation_from_checkpoint(durable.task("r"), "s")
            if armed[0] and current.leaf == "control_ready":
                armed[0] = False
                raise SimulatedExit()

        durable.subscribe_commits(committed)
        with pytest.raises(SimulatedExit):
            run_task_kernel(session, request=request)
        assert session.last_done_args == selection
        saved = KernelContext(session, request).decision(operation_from_checkpoint(durable.task("r"), "s"))
        report = FindingReport.from_payload(saved["result"]["research_report"])
        assert report == selected.research_report
        with monkeypatch.context() as frozen:
            frozen.setattr("codey.operations.research_completion_checks.compile_finding_report",
                           lambda *a, **k: pytest.fail("accepted report was recompiled"))
            frozen.setattr(store.repository, "read", lambda *a, **k: pytest.fail("accepted replay read knowledge"))
            resumed = strict_session(original_session)
            result = run_task_kernel(resumed, request=request)
            assert result.completed, result.summary
            assert result.summary == report.text and result.research_report == report.to_payload()
            assert resumed.last_done_args == selection and len(sends) == 1
        history = ResearchContinuation(mutations, outputs, session_id="s", run_id="r")
        kwargs = dict(session_id="first", project="", run_id="r", on_event=lambda _: None,
                      policy=session.policy, accepted_report=report)
        identity = publish_synthesis(history, tools, session.task_text, result.summary, **kwargs)
        note = store.read_note(identity)
        assert note.body == result.summary and unsafe not in note.body
        assert publish_synthesis(history, tools, session.task_text, result.summary, **kwargs) == identity
        assert store.read_note(identity).to_markdown() == note.to_markdown()
        assert len([row for row in durable.effects("r")
                    if row.intent.get("tool_name") == "_publish_research_answer"]) == 1
    finally:
        durable.close()
        store.close()


def test_evidence_followup_never_compiles_a_parent_report(tmp_path, monkeypatch):
    monkeypatch.setattr("codey.operations.research_completion_checks.compile_finding_report",
                       lambda *a, **k: pytest.fail("evidence phase compiled a report"))
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="evidence_followup")
    verdict = completion_gate.evaluate(session, "Acquisition failed; the evidence gap remains.")
    assert verdict.complete and verdict.research_report is None
    assert verdict.final_text == "Acquisition failed; the evidence gap remains."
