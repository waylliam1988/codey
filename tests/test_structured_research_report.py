"""Report arguments avoid multiline wire encoding while retaining the same gate."""

import copy
import json

import pytest

from codey.operations import completion_gate
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import ResearchLedger
from codey.reviews.report_sections import REQUIRED_SECTIONS, parse_sections

URL = "https://example.com/experiment"


def arguments():
    return {"summary": "One writer commits at a time. [1]", "report_sections": {
        "evidence": ["One writer commits at a time. [1]"],
        "counter": ["未找到强反证；本轮仅检索并打开上述来源。[1]"],
        "source_quality": ["Original experiment; workload limits applicability. [1]"],
        "coverage": [{"question_ref": "q1", "state": "partial", "answer": "Writer concurrency investigated", "findings": ["f1"], "investigations": []}],
    }}


def session():
    return TaskSession(TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}),
                                  strict_research=True,
                                  required_checks=("research_sources_opened", "research_evidence_saved",
                                                   "research_report_sections")))


def plan_for(args, native):
    state = session()
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("call1", "done", args),)) if native else
             decode_text_turn(json.dumps({"tool": "done", "args": args})))
    return state, normalize_turn(reply, snapshot=build_turn_snapshot(state, native=native))


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.usefixtures("public_source_dns")
def test_structured_report_preserves_arguments_and_passes_existing_gate_only_with_evidence(native, tmp_path):
    from codey.research.finding_report import compile_finding_report
    from tests.test_research_findings import URL as source_url
    from tests.test_research_findings import execute, report_args, setup

    store, tools, execution_session, delegate = setup(tmp_path)
    execute(delegate, tools, execution_session, "open_url", {"url": source_url})
    saved = execute(delegate, tools, execution_session, "save_finding", {
        "claim": "Only one writer can write at a time.", "source_ref": "s1",
        "passage_ref": next(iter(tools.ledger.read_passages))})
    ref = json.loads(saved.model_text)["finding_ref"]
    args = report_args(ref)
    before = copy.deepcopy(args)
    try:
        state, plan = plan_for(args, native)
        assert not plan.protocol_error
        assert plan.control and plan.control.kind == "done"
        assert args == before and plan.control_args == before
        assert plan.control.body == args["summary"]  # Structure stays in arguments until completion.
        state.last_done_args = plan.control_args
        assert not completion_gate.evaluate(state, plan.control.body, context={"research_ledger": ResearchLedger()}).complete
        compiled = compile_finding_report(args, tools.ledger, store)
        verdict = completion_gate.evaluate(state, compiled.text, context={"research_ledger": tools.ledger, "research_report": compiled})
        assert verdict.complete, verdict.followup
        assert set(parse_sections(compiled.text)) == set(REQUIRED_SECTIONS)
    finally:
        store.close()


@pytest.mark.parametrize("value", [None, {}, {"evidence": ["only one section"]},
                                  {**arguments()["report_sections"], "sources": []},
                                  {**arguments()["report_sections"], "sources": [1]}])
def test_malformed_structured_reports_do_not_become_completion(value):
    _, plan = plan_for({**arguments(), "report_sections": value}, True)
    assert plan.protocol_error and plan.control is None


@pytest.mark.parametrize("native", [True, False])
def test_strict_research_rejects_summary_only_even_with_a_complete_markdown_report(native):
    from codey.reviews.report_sections import render_done_report

    args = arguments()
    report = render_done_report(args["summary"], args["report_sections"])
    state = session()
    snapshot = build_turn_snapshot(state, native=native)
    done = next(spec for spec in snapshot.frozen_specs if spec.name == "done")
    assert set(done.required) == {"summary", "report_sections"}
    assert "Alternatively" not in snapshot.contract_text
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("old-format", "done", {"summary": report}),)) if native
             else decode_text_turn(json.dumps({"tool": "done", "args": {"summary": report}})))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.control is None and "report_sections" in plan.protocol_error


@pytest.mark.parametrize("native", [True, False])
def test_ordinary_done_keeps_plain_answer_contract(native):
    state = TaskSession(TaskPolicy(grants=frozenset({"control"})))
    reply = AssistantTurn(tool_calls=(ProviderToolCall("answer", "done", {"summary": "Answer"}),))
    plan = normalize_turn(reply, snapshot=build_turn_snapshot(state, native=native))
    assert plan.control and plan.control.body == "Answer" and not plan.protocol_error


@pytest.mark.parametrize("reply", [AssistantTurn(text="A plain research report"),
                                   decode_text_turn("A plain research report")])
def test_plain_text_cannot_bypass_required_research_report_arguments(reply):
    plan = normalize_turn(reply, snapshot=build_turn_snapshot(session()))
    assert plan.control is None and "report_sections" in plan.protocol_error


def test_literal_escape_sequences_in_report_content_are_preserved():
    args = arguments()
    args["report_sections"]["coverage"][0]["answer"] = r"The query contained literal \n and Windows C:\data."
    _, plan = plan_for(args, True)
    assert plan.control
    assert plan.control_args["report_sections"]["coverage"][0]["answer"] == args["report_sections"]["coverage"][0]["answer"]


def test_grouped_citations_are_compiled_but_unobserved_numbers_still_fail():
    from codey.research.done_finalizer import finalize_done_answer
    from codey.utils.citation_scanner import citation_refs

    args = arguments()
    second = "https://example.com/replication"
    args["summary"] = "One writer commits at a time. [1, 2]"
    args["report_sections"]["evidence"] = ["One writer commits at a time. [1, 2]"]
    ledger = ResearchLedger()
    for url in (URL, second):
        ledger.record_open(url, url, "Experiment", "One writer commits at a time.")
        prepared = ledger.prepare_evidence_items(
            [{"claim": "One writer commits at a time.", "source_url": url, "excerpt": "One writer commits at a time."}],
            fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
        ledger.add_evidence_items(list(prepared.items), note_id=url)
    state, plan = plan_for(args, False)
    # Numbered sources are application output, not a model-supplied wire field.
    from codey.reviews.report_sections import render_done_report

    rendered = render_done_report(args["summary"], args["report_sections"]) + f"\n\n## 来源\n[1] Experiment - {URL}\n[2] Replication - {second}"
    finalized = finalize_done_answer(rendered, ledger)
    assert "[1][2]" in finalized.text and citation_refs(finalized.text) == {1, 2}
    assert not completion_gate.evaluate(state, finalized.text, context={"research_ledger": ledger}).complete
    bad = finalize_done_answer(rendered.replace("[1, 2]", "[1, 9]"), ledger)
    assert bad.reason == "unmapped_numeric_refs"


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("separator", ["\n", r"\n"])
def test_duplicated_full_report_is_rejected_with_specific_repair(native, separator):
    args = arguments()
    args["summary"] += separator + "## 关键证据" + separator + "One writer commits at a time. [1]"
    before = copy.deepcopy(args)
    _, plan = plan_for(args, native)
    from codey.research.report_proposal import parse_report_proposal

    assert plan.control is not None and not plan.protocol_error
    with pytest.raises(ValueError, match="only the conclusion"):
        parse_report_proposal(args)
    assert args == before
