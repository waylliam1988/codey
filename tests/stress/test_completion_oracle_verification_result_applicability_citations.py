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


def _real_research_ledger_and_report():
    from codey.research.ledger import ResearchLedger

    url = "https://example.com/helium"
    source_text = "Helium is separated from natural gas streams. 2026 supply note."
    summary = (
        "## 结论\n"
        "- Helium supply depends on gas processing. [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；需要持续追踪新供应数据。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · fresh · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium supply\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )
    ledger = ResearchLedger()
    ledger.record_search("helium supply", [{"title": "Helium article", "url": url, "snippet": "Helium supply."}])
    ledger.record_open(requested_url=url, final_url=url, title="Helium article", text=source_text)
    prepared = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body=source_text,
        note_type="fact",
    )
    assert not prepared.error
    ledger.add_evidence_items(list(prepared.items), note_id="note-1")
    return ledger, summary, url


def test_oracle_rejects_report_citing_unopened_source_ledger_unchanged():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    ledger, summary, url = _real_research_ledger_and_report()
    session = TaskSession(
        policy=TaskPolicy(
            grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
            strict_research=True,
        ),
        task_kind="research",
        project="demo",
    )
    session.record_open(url, source_id="s1")
    session.record_evidence(url, "Helium is separated from natural gas streams.")
    evidence = ExecutionEvidence()
    context = {
        "research_ledger": ledger,
        "source_ids": dict(session.source_ids),
        "question": "Research helium supply",
        "run_id": "research-citation",
    }
    verdict = evaluate(session, summary, context=context)
    assert verdict.complete is True
    view = completion_view_from_gate(
        session=session, evidence=evidence, verdict=verdict,
        research_ledger=ledger, done_text=summary,
    )
    InvariantChecker().check_completion_truthful(view)
    tampered = summary + "\n[2] Unopened - https://unopened.example/b\n"
    # Also cite the unopened source in the conclusion so the parser sees it.
    tampered = tampered.replace(
        "Helium supply depends on gas processing. [1]",
        "Helium supply depends on gas processing. [1][2]",
    )
    tampered_view = completion_view_from_gate(
        session=session, evidence=evidence, verdict=verdict,
        research_ledger=ledger, done_text=tampered,
    )
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(tampered_view)
