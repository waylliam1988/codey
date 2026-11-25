"""Retired writers cannot run; historical frozen contracts remain readable."""

import json
from pathlib import Path

import pytest

from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.provider_receipts import restore_snapshot
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.effects.replay_args import validate_replay_args_shape
from codey.toolchain.tool_spec import spec_for_tool


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("kind", ["chat", "hybrid", "research", "evidence_followup"])
def test_retired_assessor_has_no_live_spec_or_executable_call(kind, native):
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        strict_research=kind == "research", required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS)
    snapshot = build_turn_snapshot(TaskSession(policy, task_kind=kind), native=native)
    assert spec_for_tool("submit_research_review") is None
    assert "submit_research_review" not in snapshot.inventory_names
    reply = AssistantTurn(tool_calls=(ProviderToolCall("retired", "submit_research_review", {"assessment": {}}),))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls and plan.control is None


def test_retired_task_kind_is_rejected_instead_of_becoming_an_ordinary_writer():
    session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.write"})), task_kind="research_review")
    with pytest.raises(ValueError, match="assessment execution has been retired"):
        build_turn_snapshot(session)


def test_archived_snapshot_requires_new_request_and_replay_arguments_remain_readable():
    source = Path(__file__).parent / "fixtures/research_live/retired_assessment_snapshot.json"
    from codey.operations.provider_receipts import runtime_implementation_id

    raw = json.loads(source.read_text(encoding="utf-8"))
    for implementation in (raw["implementation_id"], runtime_implementation_id()):
        raw["implementation_id"] = implementation
        with pytest.raises(ValueError, match="unsupported captured turn snapshot version; start a new request"):
            restore_snapshot(raw)
    args = {"basis_digest": "a" * 64, "assessment": {"claims": [], "paragraphs": []}}
    assert validate_replay_args_shape("submit_research_review", args) == args
    with pytest.raises(ValueError, match="original basis"):
        validate_replay_args_shape("submit_research_review", {"assessment": {}})
