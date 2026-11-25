"""Report arguments avoid multiline wire encoding while retaining the same gate."""

import json

import pytest

from codey.operations import completion_gate
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.ledger import ResearchLedger

URL = "https://example.com/experiment"


def arguments():
    from tests.test_research_findings import answer_args

    return answer_args("f1")


def session():
    # Shared historical report codec; current Research uses complete answers.
    return TaskSession(TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}),
                                  strict_research=True,
                                  required_checks=("research_sources_opened", "research_evidence_saved",
                                                   "research_report_sections")),
                       task_kind="chat", task_text="Explain writer concurrency.",
                       research_questions=("What limits writer concurrency?",))


def plan_for(args, native):
    state = session()
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("call1", "done", args),)) if native else
             decode_text_turn(json.dumps({"tool": "done", "args": args})))
    return state, normalize_turn(reply, snapshot=build_turn_snapshot(state, native=native))


@pytest.mark.parametrize("value", [None, {}, {"evidence": ["only one section"]},
                                  {**arguments()["report_sections"], "sources": []},
                                  {**arguments()["report_sections"], "sources": [1]}])
def test_malformed_structured_reports_do_not_become_completion(value):
    _, plan = plan_for({**arguments(), "report_sections": value}, True)
    assert plan.protocol_error and plan.control is None


@pytest.mark.parametrize("native", [True, False])
def test_strict_research_rejects_summary_only_even_with_a_complete_markdown_report(native):
    from codey.utils.report_sections import render_done_report

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
    if kind == "research":
        assert "done" not in prepared.snapshot.tool_names
        assert "submit_research_answer" in prepared.snapshot.tool_names
        assert "Submit the whole answer" in prepared.prompt
        return
    done = next(spec for spec in prepared.snapshot.model_specs if spec.name == "done")
    structured = "report_sections" in done.required
    invalid = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "done", {}),)),
                             snapshot=prepared.snapshot)
    repair = _repair_prompt(invalid, snapshot=prepared.snapshot, native=True)
    for text in (prepared.prompt, repair):
        assert ("Call done with report_sections.coverage" in text) == structured
        if kind == "evidence_followup":
            assert "brief receipt of saved findings" in text
        else:
            assert ("summary is the complete user-facing" in text) != structured
        example = text.split("Native done arguments example (replace sample content with verified task facts): ", 1)[1]
        args = json.JSONDecoder().raw_decode(example)[0]
        assert set(done.required) <= set(args)
        assert "tool" not in args and "args" not in args


@pytest.mark.parametrize("reply", [AssistantTurn(text="A plain research report"),
                                   decode_text_turn("A plain research report")])
def test_plain_text_cannot_bypass_required_research_report_arguments(reply):
    plan = normalize_turn(reply, snapshot=build_turn_snapshot(session()))
    assert plan.control is None and "report_sections" in plan.protocol_error


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
    from codey.utils.report_sections import render_done_report

    rendered = render_done_report(args["summary"], {"evidence": ["One writer commits at a time. [1]"], "counter": ["Not investigated."], "source_quality": ["Unknown source quality."], "coverage": args["report_sections"]["coverage"]}) + f"\n\n## 来源\n[1] Experiment - {URL}\n[2] Replication - {second}"
    finalized = finalize_done_answer(rendered, ledger)
    assert "[1][2]" in finalized.text and citation_refs(finalized.text) == {1, 2}
    assert not completion_gate.evaluate(state, finalized.text, context={"research_ledger": ledger}).complete
    bad = finalize_done_answer(rendered.replace("[1, 2]", "[1, 9]"), ledger)
    assert bad.reason == "unmapped_numeric_refs"
