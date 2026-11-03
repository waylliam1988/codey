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
    from tests.test_research_findings import answer_args

    return answer_args("f1")


def session():
    return TaskSession(TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}),
                                  strict_research=True,
                                  required_checks=("research_sources_opened", "research_evidence_saved",
                                                   "research_report_sections")),
                       task_kind="research", task_text="Explain writer concurrency.",
                       research_questions=("What limits writer concurrency?",))


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
    from tests.test_research_findings import execute, selection_args, setup

    store, tools, execution_session, delegate = setup(tmp_path)
    execute(delegate, tools, execution_session, "open_url", {"url": source_url})
    saved = execute(delegate, tools, execution_session, "save_finding", {
        "claim": "Only one writer can write at a time.", "source_ref": "s1",
        "passage_ref": next(iter(tools.ledger.read_passages))})
    ref = json.loads(saved.model_text)["finding_ref"]
    args = selection_args(ref)
    before = copy.deepcopy(args)
    try:
        state, plan = plan_for(args, native)
        assert not plan.protocol_error
        assert plan.control and plan.control.kind == "done"
        assert args == before and plan.control_args == before
        assert plan.control.body == ""  # Structure stays in arguments until completion.
        state.last_done_args = plan.control_args
        assert not completion_gate.evaluate(state, plan.control.body, context={"research_ledger": ResearchLedger()}).complete
        compiled = compile_finding_report(args, tools.ledger, store, task=state.task_text)
        verdict = completion_gate.evaluate(state, compiled.text, context={
            "research_ledger": tools.ledger, "knowledge_store": store,
        })
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
    report = render_done_report("One writer commits at a time. [1]", {"evidence": ["One writer commits at a time. [1]"], "counter": ["Not investigated."], "source_quality": ["Unknown source quality."], "coverage": args["report_sections"]["coverage"]})
    state = session()
    snapshot = build_turn_snapshot(state, native=native)
    done = next(spec for spec in snapshot.frozen_specs if spec.name == "done")
    assert set(done.required) == {"report_sections"}
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


@pytest.mark.parametrize("kind", ["research", "evidence_followup", "chat"])
def test_native_completion_guidance_matches_frozen_phase_schema(kind):
    from codey.operations.kernel_preparation import prepare_kernel_turn
    from codey.operations.kernel_prompt import _repair_prompt

    state = session()
    state.task_kind = kind
    prepared = prepare_kernel_turn(state, user_task="Investigate the evidence", context_text="",
                                   task_guidance="", native=True)
    done = next(spec for spec in prepared.snapshot.model_specs if spec.name == "done")
    structured = "report_sections" in done.required
    invalid = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "done", {}),)),
                             snapshot=prepared.snapshot)
    repair = _repair_prompt(invalid, snapshot=prepared.snapshot, native=True)
    for text in (prepared.prompt, repair):
        assert ("Call done with report_sections.coverage" in text) == structured
        assert ("summary contains the complete response" in text) != structured
        example = text.split("Native done arguments example (replace sample content with verified task facts): ", 1)[1]
        args = json.JSONDecoder().raw_decode(example)[0]
        assert set(done.required) <= set(args)
        assert "tool" not in args and "args" not in args


@pytest.mark.parametrize("reply", [AssistantTurn(text="A plain research report"),
                                   decode_text_turn("A plain research report")])
def test_plain_text_cannot_bypass_required_research_report_arguments(reply):
    plan = normalize_turn(reply, snapshot=build_turn_snapshot(session()))
    assert plan.control is None and "report_sections" in plan.protocol_error


@pytest.mark.usefixtures("public_source_dns")
def test_literal_escape_sequences_in_selected_report_content_are_preserved(tmp_path):
    from codey.research.finding_report import FindingReport, compile_finding_report
    from tests.test_research_findings import URL as source_url
    from tests.test_research_findings import execute, selection_args, setup

    store, tools, state, delegate = setup(tmp_path)
    claim = r"The query contained literal \n and Windows C:\data."
    tools.search.text = claim
    try:
        execute(delegate, tools, state, "open_url", {"url": source_url})
        saved = execute(delegate, tools, state, "save_finding", {
            "claim": claim, "passage_ref": next(iter(tools.ledger.read_passages))})
        assert saved.ok, saved.model_text
        args = selection_args(json.loads(saved.model_text)["finding_ref"])
        args["report_sections"]["coverage"][0]["state"] = "evidence_found"
        _, plan = plan_for(args, True)
        assert plan.control and plan.control_args == args
        report = compile_finding_report(plan.control_args, tools.ledger, store)
        assert report.coverage[0]["answer"] == claim + " [f1]"
        assert FindingReport.from_payload(report.to_payload()).coverage == report.coverage
    finally:
        store.close()


def test_grouped_citations_are_compiled_but_unobserved_numbers_still_fail():
    from codey.utils.citation_scanner import citation_refs
    from tests.support.research_done_finalizer import finalize_done_answer

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

    rendered = render_done_report(args["summary"], {"evidence": ["One writer commits at a time. [1]"], "counter": ["Not investigated."], "source_quality": ["Unknown source quality."], "coverage": args["report_sections"]["coverage"]}) + f"\n\n## 来源\n[1] Experiment - {URL}\n[2] Replication - {second}"
    finalized = finalize_done_answer(rendered, ledger)
    assert "[1][2]" in finalized.text and citation_refs(finalized.text) == {1, 2}
    assert not completion_gate.evaluate(state, finalized.text, context={"research_ledger": ledger}).complete
    bad = finalize_done_answer(rendered.replace("[1, 2]", "[1, 9]"), ledger)
    assert bad.reason == "unmapped_numeric_refs"


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("separator", ["\n", r"\n"])
def test_duplicated_full_report_is_rejected_with_specific_repair(native, separator):
    args = arguments()
    args["summary"] = "One writer commits at a time. [1]" + separator + "## 关键证据" + separator + "One writer commits at a time. [1]"
    args["report_sections"].update(evidence=["One writer commits at a time. [f1]"], counter=["Not investigated."])
    before = copy.deepcopy(args)
    _, plan = plan_for(args, native)
    from codey.research.report_proposal import parse_report_proposal

    assert plan.control is None and plan.protocol_error
    with pytest.raises(ValueError, match="only the conclusion"):
        parse_report_proposal(args)
    assert args == before
