"""Cross-question original windows are visible, budgeted and never auto-adopted."""

import json
import subprocess
import sys

import pytest

from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.task_policy import TaskPolicy
from codey.research.context_view import local_research_context
from codey.research.local_scope import current_scope, projected_passages
from codey.runtime.core.models import ToolCall
from tests.test_research_compact_plan import run_compact
from tests.test_research_findings import TEXT, URL, execute, setup

TASK = "根据材料研究：\n1. What limits writer concurrency?\n2. Can readers coexist with a writer?\n用中文作答。"


def shared_setup(root):
    store, tools, session, _ = setup(root)
    session.task_text = TASK
    session.research_questions = tools.ledger.questions = ()
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    snapshot = build_turn_snapshot(session)
    delegate = ExecutionDelegate(session=session, research_tools=tools,
        frozen_specs={spec.name: spec for spec in snapshot.frozen_specs})
    result, ok, _ = delegate._execute_research(ToolCall("research_plan", {}))
    assert ok, result.model_text
    project_result_observation(session, result.call, result, ok=ok, research_ledger=tools.ledger)
    session._memory_results["plan"] = result
    # Acquisition contains more data than this read presents. Only its actual
    # immutable descriptors may be reused by the next question.
    tools.search.text = TEXT + "\n\nNEVER_READ_BODY " * 1000
    session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL, "limit": len(TEXT)})
    scope = current_scope(session)
    result = execute(ExecutionDelegate(session=session, research_tools=tools, research_scope=scope), tools, session,
        "submit_research_answer", {"state": "answered", "finding": {
            "claim": "根据当前材料，同一时刻只有一个写者。", "passage_ref": scope.passage_refs[0]}})
    assert result.ok, result.model_text
    session._memory_results["submit"] = result
    return store, tools, session


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_next_question_sees_pinned_original_without_new_read_or_adopting_old_finding(tmp_path, native):
    store, tools, session = shared_setup(tmp_path)
    try:
        frozen = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        scope = frozen.research_scope
        assert scope.question_ref == "q2" and scope.passage_refs
        assert scope.finding_refs == ()
        tools.search.text = "Changed remote content must not be fetched."
        before = len(tools.ledger.investigations)
        body = local_research_context(session, tools.ledger, store, scope)
        assert TEXT in body and "NEVER_READ_BODY" not in body and "Changed remote" not in body
        assert not any(row["question_ref"] == "q2" for row in tools.ledger.investigations.values())
        assert len(tools.ledger.investigations) == before
        delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        rejected, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", {"state": "answered", "findings": ["f1"]}))
        assert not ok and "outside" in rejected.model_text
        result, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", {"state": "answered", "finding": {
            "claim": "根据当前材料，读者与一个写者可以并发运行。", "passage_ref": scope.passage_refs[0]}}))
        assert ok, result.model_text
        assert result.canonical["research_answer"]["question_ref"] == "q2"
        assert tools.ledger.references["f1"].question_ref == "q1"
    finally:
        store.close()


def test_reuse_budget_keeps_whole_windows_and_exact_reference_deduplication(monkeypatch):
    monkeypatch.setattr("codey.research.local_scope.SHARED_READ_CHAR_BUDGET", 10)
    windows = [("q1", ["old-a", "old-b"]), ("q2", ["current"]), ("q1", ["old-a", "old-b"])]
    lengths = {"old-a": 4, "old-b": 4, "current": 3}
    assert projected_passages(windows, lengths, "q2", reuse=True) == ("current",)
    monkeypatch.setattr("codey.research.local_scope.SHARED_READ_CHAR_BUDGET", 11)
    assert projected_passages(windows, lengths, "q2", reuse=True) == ("current", "old-a", "old-b")
    assert projected_passages(windows, lengths, "q2", reuse=False) == ("current",)
    assert projected_passages(windows, {}, "q3", reuse=True) == ()


@pytest.mark.usefixtures("public_source_dns")
def test_omitted_window_retains_locator_and_a_frozen_old_request_is_not_recomputed(tmp_path, monkeypatch):
    store, tools, session = shared_setup(tmp_path)
    try:
        original = restore_snapshot(snapshot_description(build_turn_snapshot(session)))
        monkeypatch.setattr("codey.research.local_scope.SHARED_READ_CHAR_BUDGET", 1)
        assert not current_scope(session).passage_refs
        fresh = local_research_context(session, tools.ledger, store, current_scope(session))
        assert '"result_ref":"read"' in fresh and "locator_not_evidence" in fresh
        restored = local_research_context(session, tools.ledger, store, original.research_scope)
        assert TEXT in restored
    finally:
        store.close()


def run_shared(root, *, resume=False, native=False, boundary=""):
    return run_compact(root, native=native, resume=resume, boundary=boundary, task=TASK, reuse_answer=True)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_two_questions_publish_using_one_fetch_and_no_recovery_or_search(tmp_path, native):
    result = run_shared(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert all(row["state"] == "evidence_found" for row in result.accepted_report.coverage)
    assert result.turns == 4
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("fetch" in row for row in rows) == 1 and not any("search" in row for row in rows)
    assert not result.accepted_report.coverage[1]["investigations"]


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["provider_request_ready", "provider_response_ready", "tool_delivery_pending"])
def test_cross_question_read_projection_survives_process_restart(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.test_research_evidence_reuse import run_shared; import sys; "
              "run_shared(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=60)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_shared(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("fetch" in row for row in rows) == sum("publication" in row for row in rows) == 1
