"""Output constraints stay in the task, without becoming research questions."""

import copy

import pytest

from codey.research.finding_report import FindingReport, compile_finding_report
from codey.research.task_requirements import (
    conclusion_characters,
    coverage_requirements,
    report_requirements,
    task_requirements,
    validate_report_requirements,
    validate_task_accounting,
)
from tests.test_research_findings import URL, answer_args, execute, setup


def test_counting_and_length_repair_share_one_contract():
    from codey.operations.kernel_prompt import _completion_followup
    from codey.research.issues import ResearchIssue

    assert conclusion_characters(" 中 文 [f12]\n [2] \t") == 2
    with pytest.raises(ResearchIssue) as error:
        validate_report_requirements("中" * 961, "给出约800字的中文研究结论。", from_findings=True,
            contributions=({"question_ref": "q1", "ref": "f1", "characters": 961},))
    issue = error.value
    assert issue.code == "report_length" and "q1/f1=961" in str(issue)
    assert "save supported concise replacement" in issue.allowed_actions
    feedback = _completion_followup(str(issue), native=False, structured_report=True)
    assert "does not require a new search or write" not in feedback
    assert "Preserve the submitted question answers" not in feedback
    assert issue.to_payload()["allowed_actions"] == list(issue.allowed_actions)


@pytest.mark.usefixtures("public_source_dns")
def test_saved_count_matches_final_compilation_and_gate_keeps_structured_issue(tmp_path):
    from codey.operations.completion_gate import evaluate
    from tests.test_research_findings import selection_args
    from tests.test_research_gate_preparation import strict_session

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        claim = "同一时刻只有一个写者，读取和写入可以并发。"
        result = execute(delegate, tools, session, "save_finding", {
            "claim": claim, "passage_ref": next(iter(tools.ledger.read_passages))})
        import json

        saved = json.loads(result.model_text)
        assert saved["conclusion_characters"] == conclusion_characters(claim)
        session = strict_session(session)
        session.task_text += " 给出约100字的中文研究结论。"
        session.last_done_args = selection_args(saved["finding_ref"])
        verdict = evaluate(session, "", context={"research_ledger": tools.ledger, "knowledge_store": store,
                                               "knowledge_session_id": tools.session_id})
        assert not verdict.complete
        assert verdict.research_issue["code"] == "report_length"
        assert f"q1/f1={saved['conclusion_characters']}" in verdict.followup
    finally:
        store.close()


def test_original_r6_keeps_offsets_and_moves_only_pure_format_to_report_checks():
    task = ("我们使用 SQLite WAL。多个写者能同时提交吗？长读事务有什么限制？"
            "请实际搜索官方资料，保存证据与条件，给出约 800 字的中文研究结论，区分当前保证与旧版本限制。")
    original = task_requirements(task)
    assert original[5]["task_ref"] == "r6"
    output = report_requirements(task)
    assert len(output) == 1
    row = output[0]
    assert row["text"] == task[row["start"]:row["end"]] == "给出约 800 字的中文研究结论，"
    assert row["constraints"] == {"scope": "conclusion", "counting": "non_whitespace_without_citations",
                                  "language": "zh", "min_characters": 640, "max_characters": 960}
    refs = [unit["task_ref"] for unit in coverage_requirements(task)]
    assert refs == ["r1", "r2", "r3", "r4", "r5", "r7"]
    validate_task_accounting([{"task_refs": refs}], task)
    # Historical rows may retain r6; accepting them never changes their payload.
    old = [{"task_refs": [unit["task_ref"] for unit in original]}]
    unchanged = copy.deepcopy(old)
    validate_task_accounting(old, task)
    assert old == unchanged
    with pytest.raises(ValueError, match="missing: r7"):
        validate_task_accounting([{"task_refs": refs[:-1]}], task)


@pytest.mark.parametrize("task", [
    "用中文解释 WAL 单写者限制。", "给出约800字的中文研究结论并说明版本边界。",
    "给出约800字的中文研究结论中的实验依据。", "中文研究结论有哪些错误？",
    "请比较两个版本并用中文回答。", "写出约800字的中文研究结论是否足够？",
    "给出800字以内的中文研究报告。",
])
def test_mixed_or_unrecognized_requirements_are_never_exempted(task):
    assert report_requirements(task) == []
    assert coverage_requirements(task) == task_requirements(task)


@pytest.mark.parametrize("length,accepted", [(639, False), (640, True), (800, True), (960, True), (961, False)])
def test_conclusion_length_uses_documented_inclusive_tolerance(length, accepted):
    task = "给出约800字的中文研究结论。"
    text = "中" * length + " [f1]\n [f123] "
    if accepted:
        validate_report_requirements(text, task)
    else:
        with pytest.raises(ValueError, match="640..960"):
            validate_report_requirements(text, task)


def test_language_only_has_no_coverage_refs_but_still_has_a_report_check():
    task = "请用中文回答。"
    assert coverage_requirements(task) == []
    validate_task_accounting([{"task_refs": []}], task)
    validate_report_requirements("本轮只有一项观察，尚不能推广。", task)
    with pytest.raises(ValueError, match="primarily Chinese"):
        validate_report_requirements("English only", task)


@pytest.mark.usefixtures("public_source_dns")
def test_report_checks_canonical_conclusion_before_rendering_and_restores_original_payload(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    task = session.task_text + " 给出约100字的中文研究结论。"
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        row = args["report_sections"]["coverage"][0]
        row["answer"] = "同一时刻只有一个写者。"
        with pytest.raises(ValueError, match="Report format r2"):
            compile_finding_report(args, tools.ledger, store, task=task)
        assert store.index.count() == 1  # only the finding, no report note
        row["answer"] = "中" * 80 + " [f1]"
        compiled = compile_finding_report(args, tools.ledger, store, task=task)
        assert compiled.coverage[0]["task_refs"] == ["r1"]
        assert len(compiled.text) > 120  # sources and repeated views do not count
        payload = compiled.to_payload()
        assert FindingReport.from_payload(payload).to_payload() == payload
    finally:
        store.close()
