"""普通联网来源要求与严格 Research 检查必须分离。"""
from __future__ import annotations

from types import SimpleNamespace

from codey.operations import completion_gate as gate
from codey.policies.task_policy import TaskPolicy

_VALID_FP = "sha256:" + "a" * 64


def _session(policy: TaskPolicy, **kw) -> SimpleNamespace:
    base = dict(
        task_kind="project",
        task_text="",
        policy=policy,
        edited_files={},
        verifications=[],
        verification_forbidden=False,
        opened_sources=set(),
        evidence=[],
        search_results={},
        source_ids={},
        workspace_fingerprint="",
        workspace_revision=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_search_without_open_blocks_completion() -> None:
    policy = TaskPolicy(
        grants=frozenset({"control", "project.read", "project.write", "project.verify", "web.read"}),
        sources_open_required=True,
        required_checks=("research_sources_opened",),
    )
    sess = _session(policy, opened_sources=set())
    verdict = gate.evaluate(sess, "done summary", context=None)
    assert not verdict.complete


def test_opened_source_with_edit_and_verify_completes_ordinary_task() -> None:
    policy = TaskPolicy(
        grants=frozenset({"control", "project.read", "project.write", "project.verify", "web.read"}),
        sources_open_required=True,
        required_checks=("research_sources_opened",),
    )
    sess = _session(
        policy,
        opened_sources={"https://example.com/a"},
        edited_files={"a.py": 1},
        verifications=[{
            "command": "pytest",
            "cwd": ".",
            "revision": 1,
            "passed": True,
            "exit_code": 0,
            "workspace_revision": 1,
            "workspace_fingerprint": _VALID_FP,
        }],
        workspace_fingerprint=_VALID_FP,
        workspace_revision=1,
    )
    from codey.research.ledger import ResearchLedger

    ledger = ResearchLedger()
    ledger.record_open("https://example.com/a", "https://example.com/a", "A", "Observed source")
    verdict = gate.evaluate(sess, "done summary", context={"research_ledger": ledger})
    assert verdict.complete, f"普通任务已打开来源+修改+验证通过应完成，实际 followup={verdict.followup} proof={verdict.proof}"


def test_same_facts_strict_research_still_blocks_without_evidence_report() -> None:
    policy = TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write", "knowledge.link"}),
        strict_research=True,
        required_checks=("research_sources_opened", "research_evidence_saved", "research_report_sections"),
    )
    sess = _session(
        policy,
        task_kind="research",
        opened_sources={"https://example.com/a"},
        evidence=[],
        search_results={},
    )
    verdict = gate.evaluate(sess, "只有结论没有来源章节" if False else "plain summary without sections", context=None)
    assert not verdict.complete


def test_required_report_failure_explains_literal_newlines_without_rewriting():
    from codey.research.ledger import ResearchLedger

    url = "https://example.com/a"
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Experiment", "One writer commits at a time.")
    prepared = ledger.prepare_evidence_items(
        [{"claim": "One writer commits at a time.", "source_url": url, "excerpt": "One writer commits at a time."}],
        fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
    ledger.add_evidence_items(list(prepared.items), note_id="original")
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}), strict_research=True,
                        required_checks=("research_sources_opened", "research_evidence_saved", "research_report_sections"))
    session = _session(policy, task_kind="research", opened_sources={url})
    summary = r"## 结论\nOne writer commits at a time. [1]\n## 来源\n[1] Experiment - https://example.com/a"
    verdict = gate.evaluate(session, summary, context={"research_ledger": ledger})
    assert not verdict.complete
    assert "required check 'research_report_sections'" in verdict.followup
    assert "literal backslash+n" in verdict.followup
    assert "## 结论\n## 关键证据\n## 反证与限制" in verdict.followup
    assert "does not require another search" in verdict.followup
    assert "Collect the missing local evidence or fresh verification" not in verdict.followup
    assert "\n" not in summary  # The gate did not silently decode the submitted data.


def test_missing_evidence_and_wrong_knowledge_id_receive_specific_repair_guidance():
    from codey.completion.contract import CHECK_FAIL, CompletionCheck
    from codey.operations.research_completion_checks import research_completion_feedback

    text = research_completion_feedback(_session(TaskPolicy(grants=frozenset({"control"}))), "", None, [
        CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid"),
        CompletionCheck("research_evidence_saved", CHECK_FAIL, "research_evidence_missing"),
    ])
    assert "not a note_id" in text
    assert "type=fact" in text and "source_search" in text
    assert "hypothesis without source excerpts" in text


def test_missing_evidence_feedback_prioritizes_actual_save_over_report_formatting():
    from codey.completion.contract import CHECK_FAIL, CHECK_NOT_RUN, CompletionCheck
    from codey.operations.research_completion_checks import research_completion_feedback

    text = research_completion_feedback(_session(TaskPolicy(grants=frozenset({"control"}))), "", None, [
        CompletionCheck("research_evidence_saved", CHECK_NOT_RUN, "research_evidence_missing"),
        CompletionCheck("research_claim_bindings", CHECK_FAIL, "report_claim_binding_incomplete"),
        CompletionCheck("research_report_sections", CHECK_FAIL, "report_unmapped_numeric_refs"),
    ])
    assert "successful knowledge_write result before another done" in text
    assert "using the evidence already saved" not in text
    assert "Saved claim wording available" not in text


def test_existing_evidence_with_invalid_reference_gets_one_focused_next_action():
    from codey.completion.contract import CHECK_FAIL, CompletionCheck
    from codey.operations.research_completion_checks import research_completion_feedback

    session = _session(TaskPolicy(grants=frozenset({"control", "knowledge.read"})))
    session.last_done_args = {"knowledge_refs": [{"id": "legacy-note", "revision": 1}]}
    text = research_completion_feedback(session, "", None, [
        CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid"),
        CompletionCheck("research_claim_bindings", CHECK_FAIL, "report_claim_binding_incomplete"),
    ])
    assert "Next action: execute one knowledge_read" in text
    assert '{"args":{"id":"legacy-note"},"tool":"knowledge_read"}' in text
    assert "remaining report/evidence checks will run again" in text
    assert "Unbound report statements" not in text and "Save a scoped finding" not in text


def test_complete_sections_with_bad_source_table_get_citation_not_heading_feedback():
    from codey.completion.contract import CHECK_FAIL, CompletionCheck
    from codey.operations.research_completion_checks import research_completion_feedback

    report = "\n\n".join("## " + title + "\ncontent" for title in
                           ("结论", "关键证据", "反证与限制", "来源质量", "搜索覆盖", "来源"))
    feedback = research_completion_feedback(_session(TaskPolicy(grants=frozenset({"control"}))), report, None,
        [CompletionCheck("research_report_sections", CHECK_FAIL, "report_unmapped_numeric_refs")])
    assert "All required sections are present" in feedback
    assert '"[1] Title - https://final-url"' in feedback
    assert "Missing sections:" not in feedback


def test_gate_preserves_unbound_answer_and_requests_missing_claim_instead_of_shrinking_it():
    from codey.operations.research_completion_checks import finalize_research_text
    from codey.research.ledger import ResearchLedger

    url = "https://example.com/wal"
    ledger = ResearchLedger()
    backed, missing = "Readers do not block writers.", "Only one writer can commit at a time."
    ledger.record_open(url, url, "WAL", backed + " " + missing)
    prepared = ledger.prepare_evidence_items([{"claim": backed, "source_url": url, "excerpt": backed}],
        fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
    ledger.add_evidence_items(list(prepared.items))
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}), strict_research=True,
        required_checks=("research_sources_opened", "research_evidence_saved", "research_report_sections"))
    session = _session(policy, task_kind="research", opened_sources={url})
    text = (f"## 结论\n- {backed} [1]\n- {missing} [1]\n\n"
            f"## 关键证据\n- {backed} [1]\n\n## 反证与限制\n- 未找到强反证；只检查上述资料。[1]\n\n"
            f"## 来源质量\n- 官方资料\n\n## 搜索覆盖\n- WAL\n\n## 来源\n[1] WAL - {url}")
    context = {"research_ledger": ledger}
    assert missing in finalize_research_text(session, text, context)
    verdict = gate.evaluate(session, text, context=context)
    assert not verdict.complete and not verdict.final_text
    assert "report_claim_binding_incomplete" in verdict.followup
    assert "Repair the reported report or reference defect" in verdict.followup
    assert "Collect the missing local evidence" not in verdict.followup
    assert missing in verdict.followup and "Saved claim wording" in verdict.followup
    assert len(ledger.evidence_items) == 1  # The repair feedback cannot create evidence.
