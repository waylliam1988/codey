"""An uninvestigated answer cannot become a conclusion through report compilation."""

import copy
import json
from pathlib import Path

import pytest

from codey.research.finding_report import FindingReport, compile_finding_report
from tests.test_research_findings import URL, execute, setup

FIXTURE = Path(__file__).parent / "fixtures/research_live/q8_uninvestigated_answer.json"


def prepare(tmp_path, state="not_investigated"):
    captured = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store, tools, session, delegate = setup(tmp_path)
    execute(delegate, tools, session, "research_plan", {"questions": [
        *tools.ledger.questions, *(f"Unresolved question {index}?" for index in range(2, 8)), captured["question"]]})
    execute(delegate, tools, session, "open_url", {"url": URL, "question_ref": "q1"})
    execute(delegate, tools, session, "save_finding", {
        "claim": "Only one writer can write at a time.", "passage_ref": next(iter(tools.ledger.read_passages)),
        "question_ref": "q1"})
    coverage = [{"question_ref": "q1", "state": "evidence_found", "answer": "Only one writer can write at a time.",
                 "findings": ["f1"], "investigations": ["a1"], "task_refs": ["r1"]}]
    coverage.extend({"question_ref": f"q{index}", "state": "not_investigated", "answer": "Not investigated.",
                     "findings": [], "investigations": [], "task_refs": []} for index in range(2, 8))
    coverage.append({**captured["coverage"], "state": state})
    if state == "unanswered_after_search":
        execute(delegate, tools, session, "web_search", {"query": "WAL workload performance", "question_ref": "q8"})
        coverage[-1]["investigations"] = [next(ref for ref, row in tools.ledger.investigations.items()
                                             if row["question_ref"] == "q8")]
    return store, tools, session, {"report_sections": {"coverage": coverage}}


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("state", ["not_investigated", "unanswered_after_search"])
@pytest.mark.parametrize("supported_state", ["evidence_found", "partial"])
def test_captured_q8_cannot_spread_to_report_bindings_or_restored_payload(tmp_path, state, supported_state):
    store, tools, session, args = prepare(tmp_path, state)
    try:
        args["report_sections"]["coverage"][0]["state"] = supported_state
        original = copy.deepcopy(args)
        unsafe = args["report_sections"]["coverage"][-1]["answer"]
        compiled = compile_finding_report(args, tools.ledger, store, task=session.task_text)
        assert args == original  # The original model request is audit data, not the published answer.
        gap = compiled.coverage[-1]
        assert gap["state"] == state and gap["findings"] == []
        assert gap["investigations"] == original["report_sections"]["coverage"][-1]["investigations"]
        assert "q8" in gap["answer"] and "无法据此给出结论" in gap["answer"]
        assert ("已记录调查尝试" in gap["answer"]) == (state == "unanswered_after_search")
        assert compiled.coverage[0] == original["report_sections"]["coverage"][0]
        assert compiled.reference_metadata.keys() == {"f1"}
        assert unsafe not in compiled.text and "写入速度极快" not in compiled.text
        payload = compiled.to_payload()
        assert unsafe not in json.dumps(payload, ensure_ascii=False)
        restored = FindingReport.from_payload(json.loads(json.dumps(payload)))
        assert restored == compiled
        assert next(row for row in restored.paragraph_bindings
                    if row["path"] == "report_sections.coverage[7].answer")["text"] == gap["answer"]
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("changes,match", [
    ({"investigations": ["a1"]}, "not_investigated requires"),
    ({"state": "unanswered_after_search"}, "needs actual investigations"),
    ({"state": "unanswered_after_search", "investigations": ["a1"]}, "belongs to another question"),
    ({"state": "unanswered_after_search", "investigations": ["a999"]}, "unknown investigation"),
    ({"findings": ["f1"]}, "gap states require"),
    ({"state": "partial"}, "without any saved/read finding"),
    ({"state": "partial", "findings": ["f1"]}, "belongs to another question"),
    ({"question_ref": "q9"}, "distinct q1..q8"),
])
def test_gap_compilation_keeps_state_evidence_and_receipt_rejections(tmp_path, changes, match):
    store, tools, session, args = prepare(tmp_path)
    try:
        args["report_sections"]["coverage"][-1].update(changes)
        with pytest.raises(ValueError, match=match):
            compile_finding_report(args, tools.ledger, store, task=session.task_text)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_gap_citations_are_not_adopted_and_all_unknown_is_not_success(tmp_path):
    store, tools, session, args = prepare(tmp_path)
    try:
        coverage = args["report_sections"]["coverage"]
        coverage[-1]["answer"] += " Fabricated support [f999]."
        report = compile_finding_report(args, tools.ledger, store, task=session.task_text)
        assert "f999" not in json.dumps(report.to_payload())
        coverage[0].update(state="unanswered_after_search", findings=[])
        with pytest.raises(ValueError, match="no saved or read finding is selected"):
            compile_finding_report(args, tools.ledger, store, task=session.task_text)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("failed", [False, True])
def test_omitted_real_attempts_cannot_be_published_as_uninvestigated(tmp_path, failed):
    from codey.operations.kernel_facts import project_result_observation
    from codey.runtime.core.models import ToolCall, ToolResult

    store, tools, session, args = prepare(tmp_path, "unanswered_after_search")
    try:
        if failed:
            call = ToolCall("web_search", {"query": "unavailable performance source", "question_ref": "q8"})
            result = ToolResult(call, "Search unavailable", ok=False)
            project_result_observation(session, call, result, ok=False, research_ledger=tools.ledger)
        row = args["report_sections"]["coverage"][-1]
        row.update(state="not_investigated", investigations=[])
        original = copy.deepcopy(args)
        report = compile_finding_report(args, tools.ledger, store, task=session.task_text)
        gap = report.coverage[-1]
        assert args == original
        assert gap["state"] == "unanswered_after_search"
        assert gap["investigations"] == [ref for ref, attempt in tools.ledger.investigations.items()
                                         if attempt["question_ref"] == "q8"]
        assert len(gap["investigations"]) == (2 if failed else 1)
        assert gap["findings"] == [] and "已记录调查尝试" in gap["answer"]
        assert "q8：本轮未调查" not in report.text
        assert report.coverage[0] == original["report_sections"]["coverage"][0]
    finally:
        store.close()
