"""An honest whole-answer gap can be delivered after a required source fails."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.store import KnowledgeStore
from codey.operations.completion_context import CompletionContext
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.research.answer_contract import answer_scope, submit_answer
from codey.research.answer_report import AnswerReport
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.source_reading import render_source_read
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import explicit_web_intent
from codey.types.json_value import JsonObject, canonical_json_object

URL = "https://example.com/required-record"


def test_ideographic_list_separator_does_not_become_part_of_required_url() -> None:
    assert explicit_web_intent(f"依次 open_url {URL}、browser_snapshot、browser_read。").urls == frozenset({URL})


class _StaticSource:
    def __init__(self) -> None:
        self.fetches = 0

    def search(self, query: str, *, limit: int) -> list[JsonObject]:
        raise AssertionError("completion must not search")

    def fetch(self, url: str) -> dict[str, object]:
        self.fetches += 1
        return {"status": "ok", "url": url, "title": "Independent record",
                "text": "An independent sample has a measured weight of 37 grams."}


@pytest.mark.parametrize("state,missing,failure_code,has_original,accepted", [
    ("unresolved", False, "access_restricted", False, True),
    ("unresolved", True, "access_restricted", False, False),
    ("unresolved", False, "policy_denied", False, False),
    ("unresolved", False, "snapshot_unavailable", False, False),
    ("partial", False, "page_unavailable", True, True),
    ("answered", False, "page_unavailable", True, False),
])
def test_whole_answer_checks_actual_attempts_without_claiming_failed_sources_were_read(
    tmp_path: Path, state: str, missing: bool, failure_code: str, has_original: bool, accepted: bool,
) -> None:
    task = f"Read {URL} and report the sample weight."
    required = frozenset({URL, URL + "-unattempted"} if missing else {URL})
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read"}), strict_research=True,
                                     required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS),
                          task_kind="research", task_text=task, required_source_urls=required)
    ledger = ResearchLedger()
    session._memory_results["failed"] = ToolResult(ToolCall("open_url", {"url": URL}), "ERROR: unavailable", ok=False,
        canonical={"source_failure": {"url": URL, "detail": "HTTP 403", "code": failure_code}})
    if has_original:
        other = "https://example.com/independent-record"
        source = _StaticSource()
        outcome = ResearchSourceGateway(source, ledger).open(other)
        opened = render_source_read(outcome, ledger, offset=0, limit=6000)
        session._memory_results["original"] = ToolResult(ToolCall("open_url", {"url": other}), opened.model_text,
            ok=opened.ok, canonical=canonical_json_object({"research_observation": opened.read_observation,
                                                           "read_passages": list(opened.read_passages)}))
    markdown = ("The independent sample weighs 37 grams [e1]. " if has_original else "") + (
        "The required source was unavailable; its sample weight cannot be established.")
    args: JsonObject = {"state": state, "markdown": markdown}
    submission = submit_answer(answer_scope(session), args)
    session._memory_results["submitted"] = ToolResult(ToolCall("submit_research_answer", args), "Recorded", ok=True,
                                                       canonical={"research_submission": submission})
    store = KnowledgeStore(tmp_path)
    try:
        context = CompletionContext(research_ledger=ledger, knowledge_store=store)
        verdict = evaluate(session, markdown, context=context)
        assert verdict.complete is accepted, verdict.followup
        assert ledger.final_url_set() == ({"https://example.com/independent-record"} if has_original else set())
        if accepted:
            assert isinstance(verdict.research_report, AnswerReport)
            assert verdict.research_report.state == state
            assert all(row["url"] != URL for row in verdict.research_report.source_versions)
            restored = AnswerReport.from_payload(verdict.research_report.to_payload())
            assert restored == verdict.research_report
            assert evaluate(session, markdown, context=context).final_text == verdict.final_text
            session.policy = replace(session.policy, required_checks=(
                *session.policy.required_checks, "research_sources_opened"))
            assert not evaluate(session, markdown, context=context).complete
        else:
            assert "research_source_not_opened" in verdict.followup
    finally:
        store.close()
    if has_original:
        assert source.fetches == 1
