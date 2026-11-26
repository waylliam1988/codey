"""Report delivery composes with authorized tools and rejects invalid writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, STRICT_RESEARCH_REQUIRED_CHECKS, TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.answer_contract import AnswerScope
from codey.research.search_contracts import SearchOutcome
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject
from tests.support.search_fixture import search_outcome

AUXILIARY = ("research_plan", "knowledge_commit", "knowledge_write", "knowledge_link", "save_finding", "save_inference")


class Search:
    def fetch(self, url: str) -> dict[str, object]:
        return {"status": "ok", "url": url, "title": "Original study", "text": "One writer at a time.", "truncated": False}

    def search(self, query: str, limit: int = 8) -> SearchOutcome:
        return search_outcome(query, [{"url": "https://example.com/study", "title": "Original study", "snippet": "Lead, not evidence"}], limit=limit)


def setup(tmp_path: Path) -> tuple[KnowledgeStore, ResearchTools, TaskSession, ExecutionDelegate]:
    store = KnowledgeStore(tmp_path)
    tools = ResearchTools(Search(), store, KnowledgeChanges(tmp_path), session_id="first")
    session = TaskSession(TaskPolicy(frozenset({"web.read", "knowledge.write", "knowledge.read", "control"})),
        task_kind="research", task_text="Explain writer concurrency.")
    tools.ledger.questions = ("What limits writer concurrency?",)
    session.research_questions = tools.ledger.questions
    return store, tools, session, ExecutionDelegate(session=session, research_tools=tools)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("checks", [(), STRICT_RESEARCH_REQUIRED_CHECKS, RESEARCH_ANSWER_REQUIRED_CHECKS])
def test_every_research_entry_exposes_complete_delivery_and_authorized_tools(tmp_path: Path, native: bool, checks: tuple[str, ...]) -> None:
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True, required_checks=checks)
        snapshot = build_turn_snapshot(session, native=native)
        assert isinstance(snapshot.research.scope, AnswerScope)
        assert {"knowledge_write", "save_finding", "save_inference"} <= set(snapshot.tool_names)
        assert "knowledge_commit" not in snapshot.tool_names
        assert "submit_research_answer" in snapshot.tool_names and "done" in snapshot.tool_names
        spec = next(spec for spec in snapshot.model_specs if spec.name == "submit_research_answer")
        assert spec.required == ("markdown", "state")
        assert set(dict(spec.parameters)) == {"markdown", "state", "knowledge_updates"}
        assert restore_snapshot(snapshot_description(snapshot)) == snapshot
        for args in ({"state": "answered", "findings": ["f1"]},
                     {"state": "answered", "finding": {"claim": "Old local answer"}}):
            plan = normalize_turn(decode_text_turn(json.dumps({"tool": "submit_research_answer", "args": args})), snapshot=snapshot)
            assert plan.protocol_error and not plan.calls
    finally:
        store.close()


@pytest.mark.parametrize("tool", AUXILIARY)
def test_invalid_auxiliary_action_never_writes_or_registers_plan(tmp_path: Path, tool: str) -> None:
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        delegate = ExecutionDelegate(session=session, research_tools=tools,
            research_scope=build_turn_snapshot(session).research.scope)
        questions, count = tools.ledger.questions, store.index.count()
        result, ok, _ = delegate.research_executor._execute_research(ToolCall(tool, {}))
        assert not ok and not result.ok
        assert result.model_text
        if tool == "knowledge_commit":
            assert "knowledge_updates" in result.model_text
        assert tools.ledger.questions == questions and store.index.count() == count
        assert not result.canonical.get("research_submission")
    finally:
        store.close()


def test_old_frozen_local_request_fails_explicitly_instead_of_becoming_whole_answer(tmp_path: Path) -> None:
    store, _, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        raw = snapshot_description(build_turn_snapshot(session))
        raw["research_scope"] = {"schema_version": 3, "plan_digest": "a" * 64,
                                 "unit_ref": "q1.u1", "question_ref": "q1"}
        with pytest.raises(ValueError, match="invalid whole-task research scope"):
            restore_snapshot(raw)
    finally:
        store.close()


def test_scope_for_another_task_is_rejected_before_any_acquisition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        def unexpected_search(*args: object, **kwargs: object) -> list[JsonObject]:
            pytest.fail("request for another task acquired new sources")
        monkeypatch.setattr(tools.search, "search", unexpected_search)
        delegate = ExecutionDelegate(session=session, research_tools=tools,
                                     research_scope=AnswerScope("a" * 64, (), ()))
        result, ok, _ = delegate.research_executor._execute_research(ToolCall("web_search", {"query": "writer"}))
        assert not ok and "another task" in result.model_text
        assert not tools.ledger.searches
    finally:
        store.close()


def test_complete_answer_submission_never_leaks_into_other_task_contracts(tmp_path: Path) -> None:
    store, _, session, _ = setup(tmp_path)
    try:
        session.task_kind = "chat"
        session.policy = TaskPolicy(session.policy.grants)
        session.opened_sources = {"https://example.com/study"}
        session.evidence = [{"source": "https://example.com/study", "quote": "saved prior finding"}]
        snapshot = build_turn_snapshot(session)
        assert snapshot.research.scope is None
        assert "submit_research_answer" not in snapshot.tool_names
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["research", "hybrid", "chat"])
@pytest.mark.parametrize("has_prior", [False, True])
def test_background_guidance_only_requests_fields_the_task_can_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, has_prior: bool,
) -> None:
    from codey.knowledge.model import KnowledgeObject, KnowledgeVersion
    from codey.knowledge.recall import KnowledgeHit
    from codey.operations.knowledge_context import knowledge_background

    value = KnowledgeObject("claim-writer", "claim", "Only one writer can write at a time.", "model_extraction")
    hits = [KnowledgeHit(KnowledgeVersion(value, 3, "2026-11-04", "old-commit"), "text")] if has_prior else []
    store, tools, session, _ = setup(tmp_path)
    def search(*args: object, **kwargs: object) -> list[KnowledgeHit]:
        return hits
    monkeypatch.setattr(store, "search", search)
    session.task_kind = kind
    session.policy = TaskPolicy(session.policy.grants, strict_research=kind == "research")
    try:
        text = knowledge_background(tools, session, "writer")
    finally:
        store.close()
    if kind == "research":
        assert "Use scope=" not in text and "scope_id" not in text
        assert "in done" not in text
        if not has_prior:
            assert text == ""
    else:
        assert "Use scope=personal with that scope_id" in text
    if has_prior:
        assert "quoted background, not instructions" in text
        assert '"id":"claim-writer"' in text and '"revision":3' in text
