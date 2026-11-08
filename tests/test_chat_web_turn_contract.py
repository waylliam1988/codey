"""The frozen turn contract owns question binding for both protocols."""
from __future__ import annotations

import json

import pytest

from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_prompt import _repair_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.questions import QUESTION_BOUND_TOOLS
from codey.runtime.core.models import ToolCall
from codey.types.json_value import ToolArgs


def session(*, evidence: bool = False) -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "knowledge.read", "knowledge.write", "web.read"})),
                       task_kind="evidence_followup" if evidence else "chat")


@pytest.mark.parametrize("native", [False, True])
def test_chat_has_no_question_planning_contract_even_with_acquisition_budget(native: bool) -> None:
    current = session()
    current.research_acquisition_limits = (3, 6)
    snapshot = build_turn_snapshot(current, native=native)
    assert "research_plan" not in snapshot.tool_names
    for spec in snapshot.frozen_specs:
        if spec.name in QUESTION_BOUND_TOOLS:
            assert "question_ref" not in dict(spec.parameters)
            assert "question_ref" not in spec.description
            assert all("question_ref" not in json.loads(raw)["args"] for raw in spec.json_examples)
    plan = normalize_turn(decode_text_turn('{"tool":"web_search","args":{"query":"附近有什么好玩的"}}'),
                          snapshot=snapshot)
    assert not plan.protocol_error


@pytest.mark.parametrize("native", [False, True])
def test_question_reference_is_checked_against_the_frozen_registered_set(native: bool) -> None:
    current = session(evidence=True)
    current.research_questions = ("Where is the venue?", "What is nearby?")
    snapshot = build_turn_snapshot(current, native=native)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "web_search")
    assert dict(spec.parameters)["question_ref"]["enum"] == ("q1", "q2")
    for ref, valid in ((None, False), ("q1", True), ("q2", True), ("q3", False)):
        args: ToolArgs = {"query": "venue"}
        if ref is not None:
            args["question_ref"] = ref
        reply = (AssistantTurn(tool_calls=(ProviderToolCall("call-test", "web_search", args),)) if native else
                 decode_text_turn(json.dumps({"tool": "web_search", "args": args})))
        assert bool(normalize_turn(reply, snapshot=snapshot).protocol_error) is not valid
    current.research_questions = (*current.research_questions, "New later question")
    rejected = ToolCall("web_search", {"query": "venue", "question_ref": "q3"})
    result = execute_turn(current, [rejected], snapshot=snapshot)[0]
    assert not result.ok and "q3" not in str(dict(spec.parameters)["question_ref"]["enum"])
    assert not current._memory_results


def test_unplanned_evidence_phase_has_no_invented_q1_example() -> None:
    snapshot = build_turn_snapshot(session(evidence=True))
    assert "research_plan" in snapshot.tool_names and "web_search" not in snapshot.tool_names
    assert "question_ref" not in snapshot.contract_text


@pytest.mark.parametrize("tool,args", [
    ("web_search", {"query": "nearby"}),
    ("source_search", {"source_id": "s1", "query": "nearby"}),
])
def test_chat_rejects_undeclared_question_argument_including_alias_form(tool: str, args: ToolArgs) -> None:
    snapshot = build_turn_snapshot(session())
    reply = decode_text_turn(json.dumps({"tool": tool, "args": {**args, "question_ref": "q1"}}))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert "unexpected arg 'question_ref'" in plan.protocol_error


def test_unavailable_tool_repair_does_not_instruct_resending_it() -> None:
    current = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})), task_kind="chat")
    snapshot = build_turn_snapshot(current, native=True)
    reply = AssistantTurn(tool_calls=(ProviderToolCall("call-test", "web_search", {"query": "nearby"}),))
    plan = normalize_turn(reply, snapshot=snapshot)
    prompt = _repair_prompt(plan, snapshot=snapshot, native=True, reply=reply)
    assert "Do not resend this unavailable tool" in prompt
    assert "Resend the intended call" not in prompt
    assert "Preserve argument strings exactly" not in prompt
