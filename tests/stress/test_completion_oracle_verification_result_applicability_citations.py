"""Oracle independently rechecks verification outcome, applicability, citations.

Locks: a failing latest verification (exit 1) never passes even with valid
identity; a read-only task with not_applicable verification passes; a report
that cites an unopened URL fails even when the ledger itself is unchanged.
"""
from __future__ import annotations

import pytest

from tests.stress.oracle import InvariantChecker, InvariantViolation, completion_view_from_gate


def _passing_project_gate(tmp_path):
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.workspace.revision import workspace_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    fingerprint = workspace_fingerprint(str(tmp_path))
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, fingerprint)
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=7, workspace_fingerprint=fingerprint, cwd=".",
    )
    evidence = ExecutionEvidence(workspace_revision=7, workspace_fingerprint=fingerprint)
    context = {
        "execution_evidence": evidence,
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": "oracle-result",
        "task": "fix",
    }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is True
    return session, evidence, verdict


def test_oracle_rejects_failed_latest_verification_with_valid_identity(tmp_path):
    session, evidence, verdict = _passing_project_gate(tmp_path)
    fp = session.workspace_fingerprint
    session.record_verification(
        "python -m pytest", 1, False, exit_code=1,
        workspace_revision=7, workspace_fingerprint=fp, cwd=".",
    )
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_oracle_accepts_readonly_completion_with_not_applicable_verification():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="project",
        project="demo",
    )
    evidence = ExecutionEvidence()
    verdict = evaluate(session, "done", context=None)
    assert verdict.complete is True
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    InvariantChecker().check_completion_truthful(view)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("tamper", ["delivered_text", "source_binding"])
def test_oracle_rejects_report_citing_unopened_source_ledger_unchanged(tmp_path, tamper):
    from dataclasses import replace

    from codey.operations.completion_gate import evaluate
    from codey.research.answer_report import AnswerReport, render_answer
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from tests.test_research_findings import URL, execute, setup
    from tests.test_research_whole_answer import record_submission

    store, tools, session, delegate = setup(tmp_path)
    session.policy = replace(session.policy, strict_research=True)
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        record_submission(session, {"state": "answered", "markdown": "Readers and writers can run concurrently [e1]."})
        evidence = ExecutionEvidence()
        verdict = evaluate(session, "", context={"research_ledger": tools.ledger, "knowledge_store": store,
            "knowledge_session_id": "first", "run_id": "research-citation"})
        assert verdict.complete is True, verdict.followup
        report = verdict.research_report
        assert isinstance(report, AnswerReport)
        original_reads = dict(tools.ledger.read_passages)
        original_urls = tools.ledger.final_url_set()
        view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict,
            research_ledger=tools.ledger, done_text=report.text)
        InvariantChecker().check_completion_truthful(view)
        assert view["cited_sources"] == [URL]

        if tamper == "delivered_text":
            text = report.text + "\n[e99] Unopened - https://unopened.example/b\n"
        else:
            # Forge a self-consistent report, including a fresh digest and rendering.
            # The independent oracle must still check the actual read receipt.
            sources = tuple({**row, "url": "https://unopened.example/b"} for row in report.source_versions)
            report = replace(report, source_versions=sources,
                text=render_answer(report.markdown, sources, report.evidence_bindings))
            assert AnswerReport.from_payload(report.to_payload()) == report
            verdict = replace(verdict, research_report=report)
            text = report.text
        tampered_view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict,
            research_ledger=tools.ledger, done_text=text)
        with pytest.raises(InvariantViolation):
            InvariantChecker().check_completion_truthful(tampered_view)
        assert tools.ledger.read_passages == original_reads
        assert tools.ledger.final_url_set() == original_urls
    finally:
        store.close()
