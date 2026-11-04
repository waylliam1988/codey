"""Strict reports use short bindings; no manual note/version-copy repair."""

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.test_research_findings import report_args


def test_manual_knowledge_refs_are_rejected_by_the_frozen_report_contract():
    session = TaskSession(TaskPolicy(frozenset({"control"}), strict_research=True), task_kind="research")
    # An old question registration cannot restore the retired done/report route.
    session.research_questions = ("What limits writer concurrency?",)
    args = {"report_sections": {"coverage": report_args("f1")["report_sections"]["coverage"]},
            "knowledge_refs": [{"id": "note-id", "revision": 99}]}
    snapshot = build_turn_snapshot(session, native=True)
    assert "done" not in snapshot.tool_names
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),)), snapshot=snapshot)
    assert plan.control is None and "done" in plan.protocol_error
    assert args["knowledge_refs"][0]["id"] == "note-id"


def test_whole_answer_rejects_manual_knowledge_refs_and_global_done():
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"}), strict_research=True,
        required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS), task_kind="research", task_text="Explain writer limits.")
    snapshot = build_turn_snapshot(session, native=True)
    assert "submit_research_answer" in snapshot.tool_names and "done" not in snapshot.tool_names
    args = {"markdown": "The requested evidence is unavailable.", "state": "unresolved", "knowledge_refs": []}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("submit", "submit_research_answer", args),)),
                          snapshot=snapshot)
    assert plan.control is None and not plan.calls and "knowledge_refs" in plan.protocol_error
