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
        "coverage": ["Searched writer concurrency and opened the original experiment."],
        "sources": [f"[1] Experiment - {URL}"],
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
def test_structured_report_preserves_arguments_and_passes_existing_gate_only_with_evidence(native):
    args = arguments()
    before = copy.deepcopy(args)
    state, plan = plan_for(args, native)
    assert not plan.protocol_error
    assert plan.control and plan.control.kind == "done"
    assert args == before and plan.control_args == before
    assert set(parse_sections(plan.control.body)) == set(REQUIRED_SECTIONS)
    state.last_done_args = plan.control_args
    ledger = ResearchLedger()
    context = {"research_ledger": ledger}
    assert not completion_gate.evaluate(state, plan.control.body, context=context).complete
    ledger.record_open(URL, URL, "Experiment", "One writer commits at a time.")
    assert not completion_gate.evaluate(state, plan.control.body, context=context).complete
    prepared = ledger.prepare_evidence_items(
        [{"claim": "One writer commits at a time.", "source_url": URL, "excerpt": "One writer commits at a time."}],
        fallback_sources=[URL], fallback_claim="", fallback_body="", note_type="fact")
    ledger.add_evidence_items(list(prepared.items), note_id="fact")
    verdict = completion_gate.evaluate(state, plan.control.body, context=context)
    assert verdict.complete, verdict.followup


@pytest.mark.parametrize("value", [None, {}, {"evidence": ["only one section"]},
                                  {**arguments()["report_sections"], "sources": []},
                                  {**arguments()["report_sections"], "sources": [1]}])
def test_malformed_structured_reports_do_not_become_completion(value):
    _, plan = plan_for({**arguments(), "report_sections": value}, True)
    assert plan.protocol_error and plan.control is None


def test_literal_escape_sequences_in_report_content_are_preserved():
    args = arguments()
    args["report_sections"]["coverage"] = [r"The query contained literal \n and Windows C:\data."]
    _, plan = plan_for(args, True)
    assert plan.control
    assert args["report_sections"]["coverage"][0] in plan.control.body


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("separator", ["\n", r"\n"])
def test_duplicated_full_report_is_rejected_with_specific_repair(native, separator):
    args = arguments()
    args["summary"] += separator + "## 关键证据" + separator + "One writer commits at a time. [1]"
    before = copy.deepcopy(args)
    _, plan = plan_for(args, native)
    assert plan.control is None and "summary must contain only the conclusion" in plan.protocol_error
    assert args == before
