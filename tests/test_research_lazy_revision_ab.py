"""Research never exposes revisions, including after a failed historical read."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.answer_context import answer_context
from codey.research.answer_contract import answer_scope
from codey.research.ledger import ResearchLedger
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.safe_tool_replay import replay_args_for_tool_call
from codey.toolchain.tool_spec import spec_for_tool


def session():
    return TaskSession(TaskPolicy(frozenset({"knowledge.read", "knowledge.write", "web.read", "control"}),
                                  strict_research=True, required_checks=("research_submission",)),
                       task_kind="research", task_text="Read the old evidence; revise only when requested.")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("failed_read", [False, True])
def test_revision_absent_before_and_after_read_errors(native, failed_read):
    s = session()
    first = build_turn_snapshot(s, native=native)
    if failed_read:
        s._memory_results["bad-read"] = ToolResult(ToolCall("knowledge_read", {"id": "bad-id"}),
                                                  "ERROR: not found", ok=False)
    observed = build_turn_snapshot(s, native=native)
    assert "knowledge_commit" not in first.tool_names and "knowledge_commit" not in observed.tool_names
    assert "prepare_knowledge_revision" not in first.tool_names
    assert first.model_specs == observed.model_specs
    assert spec_for_tool("prepare_knowledge_revision") is None
    retired = normalize_turn(decode_text_turn(json.dumps({"tool": "prepare_knowledge_revision", "args": {}})),
                             snapshot=observed)
    assert retired.protocol_error
    restored = session()
    restored._memory_results = deepcopy(s._memory_results)
    assert build_turn_snapshot(restored, native=native).model_specs == observed.model_specs
    view = answer_context(s, ResearchLedger(), answer_scope(s))
    assert "Use knowledge_commit to edit existing stored objects" not in view
    assert "Research does not edit existing stored objects" in view
    assert "first call prepare_knowledge_revision" not in view


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("deny", ["grant", "controller"])
def test_direct_revision_still_obeys_permissions(native, deny):
    s = session()
    if deny == "grant":
        s.policy = replace(s.policy, grants=frozenset({"control", "knowledge.read"}))
    else:
        s.controller_denied = ("knowledge_commit",)
    assert "knowledge_commit" not in build_turn_snapshot(s, native=native).tool_names


def test_retired_preparation_cannot_replay_but_original_transactions_can():
    assert replay_args_for_tool_call(ToolCall("prepare_knowledge_revision", {})) is None
    assert replay_args_for_tool_call(ToolCall("prepare_knowledge_revision", {"changes": []})) is None
    assert spec_for_tool("knowledge_commit") is not None
