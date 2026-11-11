"""Completion references from saved knowledge and from tool receipts stay distinct."""
from __future__ import annotations

from pathlib import Path

import pytest

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.research_completion_checks import knowledge_reference_checks
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonValue

RECEIPT_REF = "task_turn_effect:8e6ff88abcbd43f1"


def browser_task(*, task_kind: str = "hybrid", edited: bool = True, browser_ok: bool = True,
                 strict: bool = False, require_knowledge: bool = False) -> TaskSession:
    checks = ("knowledge_versions",) if require_knowledge else ()
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=strict, required_checks=checks),
                          task_kind=task_kind)
    call = ToolCall("browser_read", {}, "browser-read")
    receipt = ToolResult(call, "Page body", ok=browser_ok)
    session._memory_results[RECEIPT_REF] = receipt
    project_execution_record(session, RECEIPT_REF, call, receipt, ok=browser_ok)
    if edited:
        session.record_edit("result.json")
    return session


def test_hybrid_completion_projects_only_the_reference_check() -> None:
    session = browser_task()
    refs: list[JsonValue] = [{"id": RECEIPT_REF, "revision": 1}]
    session.last_done_args = {"knowledge_refs": refs}
    assert knowledge_reference_checks(session, CompletionContext()) == []
    assert session.last_done_args["knowledge_refs"] == refs


@pytest.mark.parametrize("scope", ["chat", "no-edit", "failed-browser", "strict", "required-knowledge"])
def test_tool_receipt_is_rejected_outside_completed_browser_edit(scope: str) -> None:
    session = browser_task(task_kind="chat" if scope == "chat" else "hybrid", edited=scope != "no-edit",
                           browser_ok=scope != "failed-browser", strict=scope == "strict",
                           require_knowledge=scope == "required-knowledge")
    session.last_done_args = {"knowledge_refs": [{"id": RECEIPT_REF, "revision": 1}]}
    assert knowledge_reference_checks(session)[0].reason_code == "knowledge_reference_invalid"


@pytest.mark.parametrize("extra", [
    {"id": "task_turn_effect:unknown", "revision": 1},
    {"id": RECEIPT_REF, "revision": 99},
    {"id": RECEIPT_REF, "revision": 1, "unknown": True},
])
def test_unknown_or_malformed_reference_is_still_rejected(extra: JsonValue) -> None:
    session = browser_task()
    session.last_done_args = {"knowledge_refs": [{"id": RECEIPT_REF, "revision": 1}, extra]}
    assert knowledge_reference_checks(session)[0].reason_code == "knowledge_reference_invalid"


@pytest.mark.parametrize("revision", [1, 99])
def test_saved_knowledge_identity_keeps_version_validation(tmp_path: Path, revision: int) -> None:
    store = KnowledgeStore(tmp_path)
    try:
        source = KnowledgeObject("saved-source", "source", "Observed source.", "source",
                                 provenance={"url": "https://example.test/source"})
        store.repository.commit("seed", [KnowledgeMutation(source)])
        session = browser_task()
        refs: list[JsonValue] = [{"id": source.id, "revision": revision}, {"id": RECEIPT_REF, "revision": 1}]
        session.last_done_args = {"knowledge_refs": refs}
        check, = knowledge_reference_checks(session, CompletionContext(knowledge_store=store))
        assert check.status == ("pass" if revision == 1 else "fail")
        assert session.last_done_args["knowledge_refs"] == refs
    finally:
        store.close()
