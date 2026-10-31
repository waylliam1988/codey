"""Final reports adopt complete local findings; version links remain literal."""

import copy
import json
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.providers.text_tool_schema import text_tool_schema
from codey.research.finding_report import FindingReport, compile_finding_report
from codey.research.source_literals import validate_version_literals, version_anchors, version_mentions
from tests.test_research_findings import URL, execute, setup


def selection(refs=("f1",), *, state="evidence_found"):
    return {"report_sections": {"coverage": [{"question_ref": "q1", "state": state,
        "findings": list(refs), "task_refs": ["r1"]}]}}


def save(delegate, tools, session, claim, passage=None):
    result = execute(delegate, tools, session, "save_finding", {"claim": claim,
        "passage_ref": passage or next(iter(tools.ledger.read_passages))})
    assert result.ok, result.model_text
    return json.loads(result.model_text)


@pytest.mark.usefixtures("public_source_dns")
def test_complete_conditional_finding_is_adopted_without_sentence_splitting_or_final_rewrite(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    claim = "自 3.22.0 起，只有在 -shm 和 -wal 已存在且可读，或目录可写以创建它们时，才可只读打开。例外条件仍须保留。"
    tools.search.text = ("Beginning with version 3.22.0, a read-only WAL database can be opened if "
                         "the -shm and -wal files exist and are readable or the directory is writable to create them.")
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        receipt = save(delegate, tools, session, claim)
        args = selection()
        before = copy.deepcopy(args)
        report = compile_finding_report(args, tools.ledger, store, task=session.task_text)
        assert args == before
        assert report.coverage[0]["answer"] == claim + " [f1]"
        assert report.coverage[0]["investigations"] == ["a1"]
        assert report.reference_metadata["f1"]["claim"] == claim
        assert report.reference_metadata["f1"]["interpretation_review"] == "unreviewed"
        assert receipt["version_anchors"]["original_mentions"][0]["literal"] == "3.22.0"
        assert FindingReport.from_payload(report.to_payload()).to_payload() == report.to_payload()
    finally:
        store.close()


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.usefixtures("public_source_dns")
def test_current_native_and_text_contracts_remove_answer_and_optional_attempt_bookkeeping(tmp_path, native):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session.policy = replace(session.policy, strict_research=True)
        snapshot = build_turn_snapshot(session, native=native)
        schema = text_tool_schema(tools_from_specs(snapshot.model_specs))
        validator = Draft202012Validator(schema)
        args = selection()
        raw = {"tool": "done", "args": args}
        assert validator.is_valid(raw)
        assert normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),)), snapshot=snapshot).control
        for text in ("显著提高了并发性能。", "prior to 3.7.0; also fixes the WAL-reset bug."):
            args["report_sections"]["coverage"][0]["answer"] = text
            assert not validator.is_valid(raw)
            rejected = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),)), snapshot=snapshot)
            assert rejected.protocol_error and rejected.control is None
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_separate_version_boundary_and_bug_findings_keep_their_own_passages(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    first = ("Supporting files must remain readable. " * 15
             + "Beginning with version 3.22.0, a read-only WAL database can be opened under these conditions.")
    second = "The separately documented WAL-reset defect has its own affected versions and trigger conditions."
    tools.search.text = first + "\n\n" + second
    try:
        execute(delegate, tools, session, "open_url", {"url": URL, "limit": len(first) + 2})
        p1 = next(iter(tools.ledger.read_passages))
        execute(delegate, tools, session, "open_url", {"url": URL, "offset": len(first) + 2, "limit": len(second)})
        p2 = next(ref for ref in tools.ledger.read_passages if ref != p1)
        assert second not in tools.ledger.read_passages[p1].excerpt
        assert "3.22.0" not in tools.ledger.read_passages[p2].excerpt
        first_claim = "从 3.22.0 起，可以在文档列出的条件下只读打开 WAL 数据库。"
        second_claim = "WAL-reset 缺陷的受影响版本与触发条件须按其独立说明核对。"
        save(delegate, tools, session, first_claim, p1)
        save(delegate, tools, session, second_claim, p2)
        report = compile_finding_report(selection(("f1", "f2")), tools.ledger, store)
        assert report.coverage[0]["answer"] == first_claim + " [f1]\n\n" + second_claim + " [f2]"
        assert [(row["label"], row["locator"]) for row in report.evidence_bindings] == [
            ("f1", "passage:" + p1), ("f2", "passage:" + p2)]
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_version_elsewhere_in_source_cannot_support_a_selected_read_only_passage(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    selected = ("Applicability conditions must be checked. " * 15
                + "Beginning with version 3.22.0, a read-only WAL database may be opened subject to conditions.")
    tools.search.text = selected + "\n\nWAL was introduced in version 3.7.0."
    try:
        execute(delegate, tools, session, "open_url", {"url": URL, "limit": len(selected) + 2})
        result = execute(delegate, tools, session, "save_finding", {
            "claim": "只读 WAL 在 3.7.0 之前就已受支持。", "passage_ref": next(iter(tools.ledger.read_passages))})
        assert not result.ok and "version literal absent" in result.model_text
        assert store.index.count() == 0 and not tools.ledger.references
        good = "自 3.22.0 起，只读 WAL 的打开仍受文档列出的条件约束。"
        save(delegate, tools, session, good)
        assert compile_finding_report(selection(), tools.ledger, store).coverage[0]["answer"] == good + " [f1]"
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_matching_both_versions_does_not_certify_which_behavior_or_before_since_relation(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = ("WAL was introduced in version 3.7.0. Beginning with version 3.22.0, "
                         "a read-only WAL database may be opened under specific conditions.")
    wrong = "只读 WAL 在 3.7.0 之前就已受支持，3.22.0 与该限制无关。"
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, wrong)
        report = compile_finding_report(selection(), tools.ledger, store)
        assert report.coverage[0]["answer"] == wrong + " [f1]"
        assert report.reference_metadata["f1"]["interpretation_review"] == "unreviewed"
        assert [row["literal"] for row in report.reference_metadata["f1"]["version_mentions"]] == ["3.7.0", "3.22.0"]
        assert "未经独立语义审查" in report.text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_partial_and_gaps_are_compiled_without_a_new_free_prose_channel(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    claim = "Writers and readers can run at the same time."
    try:
        execute(delegate, tools, session, "research_plan", {"questions": [*tools.ledger.questions, "What has not been investigated?"]})
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, claim)
        args = selection(state="partial")
        args["report_sections"]["coverage"].append({"question_ref": "q2", "state": "not_investigated", "findings": [], "task_refs": []})
        report = compile_finding_report(args, tools.ledger, store)
        assert report.coverage[0]["answer"].startswith(claim + " [f1]")
        assert "尚未完整回答" in report.coverage[0]["answer"]
        assert report.coverage[1]["answer"] == "q2：本轮未调查该问题，无法据此给出结论。"
        args["report_sections"]["coverage"][1]["state"] = "unanswered_after_search"
        with pytest.raises(ValueError, match="needs actual investigations"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_explicit_invalid_attempt_is_rejected_before_automatic_receipt_projection(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, "Only one writer can write at a time.")
        args = selection()
        args["report_sections"]["coverage"][0]["investigations"] = ["a999"]
        with pytest.raises(ValueError, match="unknown investigation"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_q1_completion_requires_a_supported_replacement_not_a_final_paraphrase(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    tools.ledger.questions = ("SQLite WAL 为何存在单写者限制？",)
    tools.search.text = "Readers and writers can run concurrently."
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, "WAL 模式下读写可以并发进行。")
        with pytest.raises(ValueError, match=r"coverage\[0\].findings") as failure:
            compile_finding_report(selection(), tools.ledger, store)
        assert "supported replacement finding" in str(failure.value)
        assert "application appends an unresolved-question notice" in str(failure.value)
        partial = compile_finding_report(selection(state="partial"), tools.ledger, store)
        assert "尚未完整回答" in partial.coverage[0]["answer"]
        tools.search.text = "There can only be one writer at a time because there is only one WAL file."
        execute(delegate, tools, session, "open_url", {"url": URL})
        claim = "由于只有一个 WAL 文件，同一时刻只能有一个写者。"
        save(delegate, tools, session, claim, next(reversed(tools.ledger.read_passages)))
        report = compile_finding_report(selection(("f2",)), tools.ledger, store)
        assert report.coverage[0]["answer"] == claim + " [f2]"
        assert "WAL 模式下读写可以并发进行。" not in report.text
        assert set(tools.ledger.references) == {"f1", "f2"}
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_old_committed_finding_retries_preserve_history_but_new_adoption_checks_literals(tmp_path, monkeypatch):
    from codey.knowledge.archive import stable_identity
    from codey.research.ledger import ResearchLedger

    store, tools, session, delegate = setup(tmp_path)
    tools.search.text = "Beginning with version 3.22.0, read-only WAL databases may be opened under specific conditions."
    wrong = "只读 WAL 在 3.7.0 之前就已受支持。"
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        # Simulate the pre-change save contract, retaining the same transaction.
        with monkeypatch.context() as historical:
            historical.setattr("codey.research.source_literals.validate_version_literals", lambda *args: None)
            save(delegate, tools, session, wrong)
        identity = tools.ledger.references["f1"].note_id
        claim_id = stable_identity("claim", identity, wrong)
        original = store.repository.read(claim_id, 1).to_dict()
        assert save(delegate, tools, session, wrong)["finding_ref"] == "f1"
        assert store.repository.read(claim_id, 1).to_dict() == original
        with pytest.raises(ValueError, match="version literal absent"):
            compile_finding_report(selection(), tools.ledger, store)
        # A knowledge read in another run cannot bypass the adoption check.
        tools.ledger = ResearchLedger()
        tools.ledger.questions = ("What is the read-only WAL version boundary?",)
        read = execute(delegate, tools, session, "knowledge_read", {"id": claim_id, "revision": 1})
        assert read.ok, read.model_text
        with pytest.raises(ValueError, match="version literal absent"):
            compile_finding_report(selection(), tools.ledger, store)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_automatic_receipts_can_exceed_model_submission_cap_without_dropping_history(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, "Only one writer can write at a time.")
        attempt = tools.ledger.investigations["a1"]
        tools.ledger.investigations.update({f"a{n}": copy.deepcopy(attempt) for n in range(2, 35)})
        report = compile_finding_report(selection(), tools.ledger, store)
        assert report.coverage[0]["investigations"] == list(tools.ledger.investigations)
        assert FindingReport.from_payload(report.to_payload()).coverage == report.coverage
        session.policy = replace(session.policy, strict_research=True)
        snapshot = build_turn_snapshot(session)
        schema = text_tool_schema(tools_from_specs(snapshot.model_specs))
        explicit = selection()
        explicit["report_sections"]["coverage"][0]["investigations"] = list(tools.ledger.investigations)
        assert not Draft202012Validator(schema).is_valid({"tool": "done", "args": explicit})
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_report_format_checks_bound_claims_and_repair_never_asks_for_final_answer_prose(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    task = "说明单写者限制。给出约 100 字的中文研究结论。"
    claim = ("WAL 模式允许读者与写者同时运行，但同一时刻只有一个写者。"
             "这两种限制描述的是不同主体之间的关系：读写可以并发，并不意味着多个写者可以同时写入。"
             "上述文档行为本身不构成本机工作负载下的性能测量结果。")
    tools.search.text = claim
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        save(delegate, tools, session, "同一时刻只能有一个写者。")
        with pytest.raises(ValueError, match="conclusion has") as failure:
            compile_finding_report(selection(), tools.ledger, store, task=task)
        assert "done adopts findings verbatim and accepts no answer prose" in str(failure.value)
        save(delegate, tools, session, claim)
        report = compile_finding_report(selection(("f2",)), tools.ledger, store, task=task)
        assert report.coverage[0]["answer"] == claim + " [f2]"
    finally:
        store.close()


def test_literal_offsets_and_preview_do_not_cut_the_persisted_original():
    text = "条件。" * 250 + "Only since version 3.22.0, if its supporting files are readable. " + "例外。" * 120
    mentions = version_mentions(text)
    assert text[mentions[0]["start"]:mentions[0]["end"]] == "3.22.0"
    preview = version_anchors("自 3.22.0 起适用。", [("p1", text)])
    row = preview["original_mentions"][0]
    assert row["context"] == text[row["context_start"]:row["context_end"]]
    assert not row["context_complete"] and preview["interpretation_review"] == "unreviewed"
    validate_version_literals("v3.22.0 applies.", [text])
    with pytest.raises(ValueError, match="absent"):
        validate_version_literals("3.23.0 applies.", [text])
    repeated = version_anchors("自 3.22.0 起适用。", [("p1", "3.22.0。" * 10)])
    assert len(repeated["original_mentions"]) == 8 and not repeated["display_complete"]


def test_version_literal_boundaries_keep_sentence_periods_and_release_suffixes_separate():
    text = "从3.22.0起支持。WAL began in 3.7.0. Test v3.51.1-rc1. New 3.51.1."
    assert [row["literal"] for row in version_mentions(text)] == ["3.22.0", "3.7.0", "v3.51.1-rc1", "3.51.1"]
    with pytest.raises(ValueError, match="absent"):
        validate_version_literals("Release 3.51.1 is stable.", ["Test 3.51.1-rc1."])
