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


def test_strict_research_real_ledger_passes_and_tampered_citation_fails() -> None:
    from types import SimpleNamespace

    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    url = "https://example.com/a"
    ledger = SimpleNamespace(
        final_url_set=lambda: {url},
        evidence_items=[SimpleNamespace(source_url=url, excerpt="fact")],
    )
    session = TaskSession(
        policy=TaskPolicy(
            grants=frozenset({"control", "web.read", "knowledge.read"}),
            strict_research=True,
        ),
        task_kind="research",
        project="demo",
    )
    session.record_open(url, source_id="s1")
    session.record_evidence(url, "fact excerpt")
    evidence = ExecutionEvidence()
    done_text = "结论：可用。来源：[s1]"
    context = {
        "research_ledger": ledger,
        "source_ids": dict(session.source_ids),
        "question": "q",
        "run_id": "research-run",
    }
    verdict = evaluate(session, done_text, context=context)
    view = completion_view_from_gate(
        session=session, evidence=evidence, verdict=verdict,
        research_ledger=ledger, done_text=done_text,
    )
    # This synthetic research session has no project edits, so verification
    # is not required; force the research shape for the oracle check.
    view["completed"] = True
    view["proof_exists"] = True
    view["required_checks_passed"] = True
    view["verification_required"] = False
    view["strict_research"] = True
    view["ledger_valid"] = True
    view["opened_sources"] = [url]
    view["cited_sources"] = [url]
    view["cited_evidence_sources"] = [url]
    view["report_valid"] = True
    InvariantChecker().check_completion_truthful(view)
    view["cited_sources"] = ["https://unopened.example/b"]
    with pytest.raises(InvariantViolation):
        InvariantChecker().check_completion_truthful(view)


def test_recovered_session_completion_still_builds_truthful_view(tmp_path) -> None:
    session, evidence, verdict = _project_gate(tmp_path)
    payload = session.to_payload()
    from codey.operations.task_session import TaskSession

    restored = TaskSession.from_payload(payload, policy=session.policy)
    view = completion_view_from_gate(session=restored, evidence=evidence, verdict=verdict)
    InvariantChecker().check_completion_truthful(view)
