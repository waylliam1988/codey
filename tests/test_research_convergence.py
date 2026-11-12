"""Research has one complete-answer contract and rejects retired local writes."""

import json
from types import SimpleNamespace

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, STRICT_RESEARCH_REQUIRED_CHECKS, TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.answer_contract import AnswerScope
from codey.runtime.core.models import ToolCall
from tests.test_research_findings import setup

RETIRED = ("research_plan", "knowledge_commit", "knowledge_write", "knowledge_link", "save_finding", "save_inference")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("checks", [(), STRICT_RESEARCH_REQUIRED_CHECKS, RESEARCH_ANSWER_REQUIRED_CHECKS])
def test_every_research_entry_exposes_only_complete_answer_contract(tmp_path, native, checks):
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True, required_checks=checks)
        snapshot = build_turn_snapshot(session, native=native)
        assert isinstance(snapshot.research_scope, AnswerScope)
        assert not set(RETIRED).intersection(snapshot.tool_names)
        assert "submit_research_answer" in snapshot.tool_names and "done" not in snapshot.tool_names
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


@pytest.mark.parametrize("tool", RETIRED)
def test_forced_retired_action_never_writes_or_registers_plan(tmp_path, tool):
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        delegate = ExecutionDelegate(session=session, research_tools=tools,
            research_scope=build_turn_snapshot(session).research_scope)
        questions, count = tools.ledger.questions, store.index.count()
        result, ok, _ = delegate._execute_research(ToolCall(tool, {}))
        assert not ok and not result.ok
        assert "knowledge_updates" in result.model_text
        assert tools.ledger.questions == questions and store.index.count() == count
        assert not result.canonical.get("research_submission")
    finally:
        store.close()


def test_old_frozen_local_request_fails_explicitly_instead_of_becoming_whole_answer(tmp_path):
    store, _, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        raw = snapshot_description(build_turn_snapshot(session))
        raw["research_scope"] = {"schema_version": 3, "plan_digest": "a" * 64,
                                 "unit_ref": "q1.u1", "question_ref": "q1"}
        with pytest.raises(ValueError, match="legacy local Research request.*start a new"):
            restore_snapshot(raw)
    finally:
        store.close()


def test_scope_for_another_task_is_rejected_before_any_acquisition(tmp_path, monkeypatch):
    store, tools, session, _ = setup(tmp_path)
    try:
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        monkeypatch.setattr(tools.search, "search", lambda *_: pytest.fail("retired request acquired new sources"))
        delegate = ExecutionDelegate(session=session, research_tools=tools,
                                     research_scope=AnswerScope("a" * 64, (), ()))
        result, ok, _ = delegate._execute_research(ToolCall("web_search", {"query": "writer"}))
        assert not ok and "another task" in result.model_text
        assert not tools.ledger.searches
    finally:
        store.close()


def test_complete_answer_submission_never_leaks_into_other_task_contracts(tmp_path):
    store, _, session, _ = setup(tmp_path)
    try:
        session.task_kind = "chat"
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        session.opened_sources = {"https://example.com/study"}
        session.evidence = [("https://example.com/study", "saved prior finding")]
        snapshot = build_turn_snapshot(session)
        assert snapshot.research_scope is None
        assert "submit_research_answer" not in snapshot.tool_names
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["research", "hybrid", "chat"])
@pytest.mark.parametrize("has_prior", [False, True])
def test_background_guidance_only_requests_fields_the_task_can_submit(kind, has_prior):
    from codey.knowledge.model import KnowledgeObject, KnowledgeVersion
    from codey.knowledge.recall import KnowledgeHit
    from codey.operations.knowledge_context import knowledge_background
    from codey.operations.task_session import TaskSession

    value = KnowledgeObject("claim-writer", "claim", "Only one writer can write at a time.", "model_extraction")
    hits = [KnowledgeHit(KnowledgeVersion(value, 3, "2026-11-04", "old-commit"), "text")] if has_prior else []
    tools = SimpleNamespace(store=SimpleNamespace(search=lambda *args, **kwargs: hits, owner_id="owner"),
                            project="", session_id="s")
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read", "knowledge.write"}),
                                    strict_research=kind != "chat"), task_kind=kind)
    text = knowledge_background(tools, session, "writer")
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
