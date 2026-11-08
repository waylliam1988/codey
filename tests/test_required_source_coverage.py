"""Required sources share one deterministic coverage in context and completion."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.source_gaps import task_source_coverage
from codey.operations.task_session import TaskSession
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_source_failure_closure import REF, URL, source_session

OTHER = "https://other.example.com/"


def opened(session: TaskSession, ledger: ResearchLedger, url: str, *, final: str = "") -> None:
    ledger.record_open(url, final or url, "A readable page", "actual readable body")
    session.record_open(final or url)
    session._memory_results[f"opened:{url}"] = ToolResult(ToolCall("open_url", {"url": url}), "actual readable body",
        ok=True, canonical={"research_observation": ledger_observation(ledger, "open", url=final or url)})


@pytest.mark.parametrize("include_claims", [False, True])
def test_partial_success_closes_as_blocked_with_or_without_model_gap_copy(include_claims: bool) -> None:
    session = source_session()
    session.required_source_urls |= {OTHER}
    if not include_claims:
        session.last_done_args = {}
    ledger = ResearchLedger()
    opened(session, ledger, OTHER)
    coverage = task_source_coverage(session, ledger)
    assert not coverage.satisfied and coverage.unavailable
    assert [row.url for row in coverage.failures] == [URL]
    verdict = evaluate(session, "已读到部分内容；另一来源无法读取", context={"research_ledger": ledger})
    assert verdict.blocked and not verdict.complete


def test_unattempted_required_url_cannot_be_hidden_by_an_unrelated_open() -> None:
    session = source_session(actual_failure=False)
    session.last_done_args = {}
    ledger = ResearchLedger()
    opened(session, ledger, OTHER)
    assert task_source_coverage(session, ledger).missing == (URL,)
    verdict = evaluate(session, "读到了另一个页面", context={"research_ledger": ledger})
    assert not verdict.complete and not verdict.blocked
    assert URL in verdict.followup


@pytest.mark.parametrize("later_failure", [False, True])
def test_success_resolves_failed_request_without_permanent_failure_block(later_failure: bool) -> None:
    session = source_session()
    session.last_done_args = {}
    ledger = ResearchLedger()
    opened(session, ledger, URL)
    if later_failure:
        session._memory_results["later_failure"] = session._memory_results[REF]
    coverage = task_source_coverage(session, ledger)
    assert coverage.satisfied and not coverage.failures
    assert evaluate(session, "已经读到页面内容", context={"research_ledger": ledger}).complete


def test_redirects_and_merged_request_aliases_remain_covered() -> None:
    session = source_session()
    session.required_source_urls |= {OTHER}
    session.last_done_args = {}
    ledger = ResearchLedger()
    final = "https://final.example.com/article"
    opened(session, ledger, URL, final=final)
    opened(session, ledger, OTHER, final=final)
    assert task_source_coverage(session, ledger).satisfied
    assert not task_source_coverage(session, ledger).failures


def test_admission_snapshot_survives_reopen_and_prompt_changes(tmp_path: Path) -> None:
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.durable.errors import CommitConflict
    from codey.operations.context import RunWork
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from tests.support.task_entry_helpers import entry_frame, submission

    path = tmp_path / "durable.sqlite3"
    durable = DurableSession(SQLiteDurableStore(path))
    durable.admit_input(conversation_id="s", request_id="q", task_id="r", text=f"打开 {URL} 和 {OTHER}",
                         provider_snapshot={"provider_id": "local"})
    reopened = DurableSession(SQLiteDurableStore(path))
    frame = entry_frame(replace(submission("resume: prompt contains no URL"), project=None))
    policy = build_task_policy_for_entry(frame.request, "chat")
    session = start_task_session(frame, RunWork([], ExecutionEvidence()), policy, "chat", durable=reopened)
    assert session.required_source_urls == {URL, OTHER}
    session.task_text = "updated display text"
    assert session.clone_projection().required_source_urls == {URL, OTHER}
    with pytest.raises(CommitConflict, match="immutable"):
        reopened.advance_task(task_id="r", phase="dispatching",
                              state={"source_requirements": {"version": 1, "required_urls": []}})


def test_legacy_requirements_use_stored_input_and_reject_invalid_snapshots() -> None:
    from codey.task.web_intent import restored_source_requirements

    assert restored_source_requirements({"input_text": f"打开 {URL}"}) == {URL}
    assert restored_source_requirements({"task": f"打开 {OTHER}"}) == {OTHER}
    for snapshot in ({"version": True, "required_urls": []}, {"version": 2, "required_urls": []},
                     {"version": 1, "required_urls": [7]}, {"version": 1}):
        with pytest.raises(ValueError):
            restored_source_requirements({"source_requirements": snapshot})
