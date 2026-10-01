"""Oracle real-run wiring: views come from gate facts, not verdict claims.

Locks that a real project edit plus verification plus ``done`` builds a
passing truthfulness view, that ``assert_valid`` actually calls the oracle,
and that tampered verification identity or ledger citations fail even when
the view still claims completion.
"""
from __future__ import annotations

import pytest

from tests.stress.oracle import InvariantChecker, InvariantViolation, completion_view_from_gate


def _project_gate(tmp_path):
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
        "run_id": "real-run",
        "task": "fix",
    }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is True
    return session, evidence, verdict


def test_real_project_run_builds_passing_view(tmp_path) -> None:
    session, evidence, verdict = _project_gate(tmp_path)
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    InvariantChecker().check_completion_truthful(view)


def test_assert_valid_calls_truthful_oracle_for_real_view(tmp_path) -> None:
    session, evidence, verdict = _project_gate(tmp_path)
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    calls: list[dict] = []
    checker = InvariantChecker()
    original = checker.check_completion_truthful
    def _track(v: dict) -> None:
        calls.append(v)
        return original(v)
    checker.check_completion_truthful = _track  # type: ignore[method-assign]
    checker.assert_valid({"log_rows": [], "ghost_rows": []}, completion_views=[view])
    assert len(calls) == 1
    # facts-carried views take the same path.
    checker.assert_valid({"log_rows": [], "ghost_rows": [], "completion_views": [view]})
    assert len(calls) == 2


def test_tampered_verification_identity_fails_oracle(tmp_path) -> None:
    session, evidence, verdict = _project_gate(tmp_path)
    view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    view["verification_fingerprint"] = "sha256:" + "b" * 64
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def _real_research_gate():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.research.ledger import ResearchLedger
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

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
        "run_id": "research-run",
    }
    verdict = evaluate(session, summary, context=context)
    assert verdict.complete is True
    return session, evidence, verdict, ledger, summary


def test_strict_research_real_ledger_passes_and_tampered_citation_fails() -> None:
    session, evidence, verdict, ledger, summary = _real_research_gate()
    view = completion_view_from_gate(
        session=session, evidence=evidence, verdict=verdict,
        research_ledger=ledger, done_text=summary,
    )
    InvariantChecker().check_completion_truthful(view)
    tampered = summary + "\n[2] Unopened - https://unopened.example/b\n"
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


def _explicit_copy_session(session):
    import copy

    from codey.operations.task_session import TaskSession

    copied = TaskSession(
        policy=session.policy,
        task_kind=session.task_kind,
        project=session.project,
        max_turns=session.max_turns,
    )
    copied.edited_files = copy.deepcopy(session.edited_files)
    copied.verifications = copy.deepcopy(session.verifications)
    copied.read_files = set(session.read_files)
    copied.workspace_revision = session.workspace_revision
    copied.workspace_fingerprint = session.workspace_fingerprint
    return copied


def test_recovered_session_completion_still_builds_truthful_view(tmp_path) -> None:
    from codey.operations.completion_gate import evaluate

    session, evidence, verdict = _project_gate(tmp_path)
    # Explicit state copy: same factory plus copied facts, no restore path.
    copied = _explicit_copy_session(session)
    assert copied.verifications == session.verifications
    assert copied.edited_files == session.edited_files
    assert copied.workspace_revision == session.workspace_revision
    assert copied.workspace_fingerprint == session.workspace_fingerprint
    # Gate keeps the same verdict on the copied state.
    copied_verdict = evaluate(copied, "done", context=None)
    assert copied_verdict.complete is True
    # Oracle behavior is consistent across the explicit copy.
    original_view = completion_view_from_gate(session=session, evidence=evidence, verdict=verdict)
    InvariantChecker().check_completion_truthful(original_view)
    view = completion_view_from_gate(session=copied, evidence=evidence, verdict=copied_verdict)
    InvariantChecker().check_completion_truthful(view)
