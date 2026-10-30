"""Strict reports use short bindings; no manual note/version-copy repair."""

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.test_research_findings import report_args


def test_manual_knowledge_refs_are_rejected_by_the_frozen_report_contract():
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research")
    args = {**report_args("f1"), "knowledge_refs": [{"id": "note-id", "revision": 99}]}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),)),
                          snapshot=build_turn_snapshot(session, native=True))
    assert plan.control is None and "knowledge_refs" in plan.protocol_error
    assert args["knowledge_refs"][0]["id"] == "note-id"
