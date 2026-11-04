"""Replay the real q1 diversion through exact saved passages and compilation."""

import pytest

from codey.research.finding_report import compile_finding_report
from codey.research.issues import ResearchIssue
from tests.test_research_findings import URL, answer_args, execute, setup

QUESTION = "官方文档是否保证同一时刻只有一个写者？"
COEXISTENCE = ("WAL provides more concurrency as readers do not block writers and "
               "a writer does not block readers. Reading and writing can proceed concurrently.")
LIMIT = ("Writers and readers can run at the same time. However, since there is only one WAL file, "
         "there can only be one writer at a time.")
DIVERSION = "官方文档保证读者不阻塞写入者，写入者不阻塞读者，多个读者可以与一个写入者并发操作。"
ANSWER = "由于只有一个 WAL 文件，同一时刻只能有一个写者。"


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("source,answer,state,accepted", [
    (COEXISTENCE, DIVERSION, "evidence_found", False),
    (COEXISTENCE, DIVERSION, "partial", False),
    (COEXISTENCE, ANSWER, "evidence_found", False),
    (COEXISTENCE, ANSWER, "partial", False),
    (COEXISTENCE, "WAL 存在单写者限制。", "evidence_found", False),
    (LIMIT, DIVERSION, "evidence_found", False),
    (LIMIT, ANSWER, "evidence_found", True),
    ("There can only be one writer at a time.", ANSWER, "evidence_found", True),
    (COEXISTENCE, "读写可以并发；同一时刻的写者数量上限尚未确定。", "partial", True),
])
def test_count_question_cannot_be_closed_with_only_general_coexistence(tmp_path, source, answer, state, accepted):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = session.research_questions = (QUESTION,)
    tools.search.text = source
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        # Related findings may still be archived; the guard belongs to completion.
        saved = execute(delegate, tools, session, "save_finding", {"claim": answer,
            "passage_ref": next(iter(tools.ledger.read_passages))})
        assert saved.ok, saved.model_text
        args = answer_args("f1")
        args["report_sections"]["coverage"][0].update(answer=answer, state=state)
        if accepted:
            report = compile_finding_report(args, tools.ledger, store, task=session.task_text)
            assert report.reference_metadata["f1"]["interpretation_review"] == "unreviewed"
        else:
            with pytest.raises(ResearchIssue) as failure:
                compile_finding_report(args, tools.ledger, store, task=session.task_text)
            issue = failure.value
            assert issue.code == "count_limit_not_addressed"
            assert QUESTION in issue.message
            assert issue.requires_acquisition == (source == COEXISTENCE)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_actual_coexistence_question_can_be_answered_by_coexistence_evidence(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = session.research_questions = ("多个读者能和一个写者同时操作吗？",)
    tools.search.text = COEXISTENCE
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": DIVERSION,
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        args["report_sections"]["coverage"][0].update(answer=DIVERSION, state="evidence_found")
        assert compile_finding_report(args, tools.ledger, store).coverage[0]["answer"] == DIVERSION
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_limit_cue_does_not_certify_the_number_or_selected_claim(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = session.research_questions = (QUESTION,)
    tools.search.text = LIMIT
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": "There can only be two writers at a time.",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        args["report_sections"]["coverage"][0].update(answer="同一时刻只能有两个写者。", state="evidence_found")
        report = compile_finding_report(args, tools.ledger, store)
        assert report.reference_metadata["f1"]["interpretation_review"] == "unreviewed"
        assert "未经独立语义审查" in report.text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_actual_focused_run_single_actor_wording_still_needs_direct_limit_evidence(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = session.research_questions = (
        "SQLite 的 WAL 模式下，是否存在单写者限制（即是否允许同时存在多个写操作）？",)
    tools.search.text = COEXISTENCE
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": ANSWER,
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        args["report_sections"]["coverage"][0].update(answer=ANSWER, state="evidence_found")
        with pytest.raises(ResearchIssue) as failure:
            compile_finding_report(args, tools.ledger, store)
        assert failure.value.code == "count_limit_not_addressed"
        assert failure.value.requires_acquisition
    finally:
        store.close()



@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("state", ["evidence_found", "partial"])
def test_archived_report_feedback_keeps_shared_count_check(tmp_path, state):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = session.research_questions = (QUESTION,)
    tools.search.text = COEXISTENCE
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": ANSWER,
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        args["report_sections"]["coverage"][0].update(answer=ANSWER, state=state)
        with pytest.raises(ResearchIssue) as failure:
            compile_finding_report(args, tools.ledger, store)
        issue = failure.value
        assert issue.code == "count_limit_not_addressed"
        assert "use partial" in issue.message
        assert "complete answer" not in issue.message
        assert "source_search" not in issue.message and "open_hit" not in issue.message
        assert "select partial or unresolved" in issue.allowed_actions
        assert "submit unresolved with findings=[]" not in issue.allowed_actions
    finally:
        store.close()
