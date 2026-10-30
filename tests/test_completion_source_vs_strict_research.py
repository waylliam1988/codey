"""普通联网来源要求与严格 Research 检查必须分离。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

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
    verdict = gate.evaluate(sess, "plain summary without sections", context=None)
    assert not verdict.complete


def test_strict_report_does_not_parse_or_rewrite_markdown_even_with_saved_evidence():
    from codey.operations.research_completion_checks import finalize_research_text, strict_research_checks
    from codey.research.ledger import ResearchLedger

    url = "https://example.com/a"
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Experiment", "One writer commits at a time.")
    prepared = ledger.prepare_evidence_items(
        [{"claim": "One writer commits at a time.", "source_url": url, "excerpt": "One writer commits at a time."}],
        fallback_sources=[url], fallback_claim="", fallback_body="", note_type="fact")
    ledger.add_evidence_items(list(prepared.items), note_id="original")
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.write"}), strict_research=True)
    session = _session(policy, task_kind="research", opened_sources={url})
    for text in (r"## Conclusion\nOne writer commits at a time. [1]", "## Conclusion\nOne writer commits at a time. [1]"):
        context = {"research_ledger": ledger}
        assert finalize_research_text(session, text, context) == text
        rows = strict_research_checks(session, text, context)
        assert [(row.check_id, row.reason_code) for row in rows] == [
            ("research_report_sections", "structured_report_missing")]
        assert not gate.evaluate(session, text, context=context).complete
    assert len(ledger.evidence_items) == 1


def test_ordinary_reference_repair_does_not_demand_unavailable_acquisition_tools():
    from codey.completion.contract import CHECK_FAIL, CompletionCheck
    from codey.operations.research_completion_checks import research_completion_feedback

    session = _session(TaskPolicy(grants=frozenset({"control", "knowledge.read"})))
    feedback = research_completion_feedback(session, "", None, [
        CompletionCheck("knowledge_versions", CHECK_FAIL, "knowledge_reference_invalid"),
    ])
    assert "exact object id and revision actually read" in feedback
    assert "not an old answer" in feedback
    assert "web_search" not in feedback and "save_finding" not in feedback and "knowledge_write" not in feedback


@pytest.mark.usefixtures("public_source_dns")
def test_strict_compiler_cannot_use_opened_pages_without_adopting_saved_knowledge(tmp_path):
    from codey.research.finding_report import compile_finding_report
    from tests.test_research_findings import URL, execute, report_args, setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        with pytest.raises(ValueError):
            compile_finding_report(report_args("f1"), tools.ledger, store)
        assert not tools.ledger.references and not tools.ledger.evidence_items
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_generated_source_table_uses_observed_redirect_and_rejects_manual_table(tmp_path, monkeypatch):
    import json

    from codey.research.finding_report import compile_finding_report
    from codey.research.issues import ResearchIssue
    from tests.test_research_findings import TEXT, URL, execute, report_args, setup

    store, tools, session, delegate = setup(tmp_path)
    final = URL + "-current"
    monkeypatch.setattr(tools.search, "fetch", lambda url: {
        "status": "ok", "url": final, "title": "Redirected study", "text": TEXT, "truncated": False})
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        assert saved.ok, saved.model_text
        args = report_args(json.loads(saved.model_text)["finding_ref"])
        compiled = compile_finding_report(args, tools.ledger, store)
        assert compiled.source_versions[0]["url"] == final
        assert "[1] Redirected study - " + final in compiled.text
        args["report_sections"]["sources"] = ["[1] Invented - https://other.example/"]
        with pytest.raises(ResearchIssue) as error:
            compile_finding_report(args, tools.ledger, store)
        assert error.value.path == "report_sections"
        assert "https://other.example/" not in compiled.text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_counter_repair_is_local_and_cannot_create_evidence(tmp_path):
    import copy
    import json

    from codey.research.finding_report import compile_finding_report
    from codey.research.issues import ResearchIssue
    from tests.test_research_findings import URL, execute, report_args, setup

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        ref = json.loads(saved.model_text)["finding_ref"]
        args = report_args(ref)
        args["report_sections"]["coverage"][0].update(state="evidence_found", answer="Writer concurrency is limited.")
        args["report_sections"]["counter"] = ["Only one writer can write at a time."]
        before = copy.deepcopy(args)
        with pytest.raises(ResearchIssue) as error:
            compile_finding_report(args, tools.ledger, store)
        assert error.value.code == "counter_basis_missing"
        assert error.value.path == "report_sections.counter[0]" and not error.value.requires_acquisition
        assert args == before and len(tools.ledger.evidence_items) == 1
        args["report_sections"]["counter"][0] += f" [{ref}]"
        assert compile_finding_report(args, tools.ledger, store).knowledge_refs
        assert len(tools.ledger.evidence_items) == 1
    finally:
        store.close()
