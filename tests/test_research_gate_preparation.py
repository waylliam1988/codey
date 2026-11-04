"""Complete-answer gate preparation precedes citations and accepted replay is frozen."""

import copy
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.operations import completion_gate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from tests.test_research_findings import URL, execute, setup
from tests.test_research_whole_answer import record_submission
from tests.test_research_whole_kernel import run_whole

pytestmark = pytest.mark.usefixtures("public_source_dns")


def test_gate_keeps_invalid_submission_and_prepares_before_citations(tmp_path, monkeypatch):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
        session.policy = TaskPolicy(session.policy.grants, strict_research=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS)
        record_submission(session, {"state": "answered", "markdown": "Only one writer can write at a time [e1]."})
        original = copy.deepcopy(session._memory_results)
        context = {"research_ledger": tools.ledger, "knowledge_store": store, "knowledge_session_id": tools.session_id}
        calls = []
        citations = completion_gate.citation_requirement_checks

        def checked(session, text, context, *, report=None):
            assert report is not None and text == report.text
            calls.append(report)
            return citations(session, text, context, report=report)

        monkeypatch.setattr(completion_gate, "citation_requirement_checks", checked)
        saved = session._memory_results["submitted"]
        bad = copy.deepcopy(saved.canonical)
        bad["research_submission"]["markdown"] = "Changed answer [e1]."
        session._memory_results["submitted"] = replace(saved, canonical=bad)
        rejected = completion_gate.evaluate(session, "original draft", context=context)
        assert not rejected.complete and rejected.research_report is None
        assert "frozen input" in rejected.followup and not calls
        assert session._memory_results["submitted"].canonical == bad
        session._memory_results["submitted"] = saved
        accepted = completion_gate.evaluate(session, "original draft", context=context)
        assert accepted.complete, accepted.followup
        assert calls == [accepted.research_report] and accepted.final_text == accepted.research_report.text
        assert session._memory_results == original and session.last_done_text == ""
        assert "research_report" not in context
    finally:
        store.close()


@pytest.mark.parametrize("boundary", ["kernel_settled", "publication_admitted", "publication_receipt"])
def test_accepted_answer_restart_never_recompiles_or_republishes(tmp_path, monkeypatch, boundary):
    source = ("from pathlib import Path; import sys; from tests.test_research_whole_kernel import run_whole; "
              "run_whole(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    monkeypatch.setattr("codey.research.answer_report.compile_answer_report",
                        lambda *a, **k: pytest.fail("accepted answer was recompiled"))
    result = run_whole(tmp_path, resume=True)
    assert result.stop_reason == "done" and result.accepted_report.markdown in result.summary
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_whole(tmp_path, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


def test_evidence_followup_never_compiles_a_parent_report(tmp_path, monkeypatch):
    monkeypatch.setattr("codey.operations.research_completion_checks.compile_finding_report",
                       lambda *a, **k: pytest.fail("evidence phase compiled a report"))
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="evidence_followup")
    verdict = completion_gate.evaluate(session, "Acquisition failed; the evidence gap remains.")
    assert verdict.complete and verdict.research_report is None
    assert verdict.final_text == "Acquisition failed; the evidence gap remains."
