"""Web/file boundaries and optional-action repair preserve completion contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.operations.kernel_prompt import _repair_prompt
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.toolchain.runtime import read_file
from codey.types.json_value import ToolArgs
from tools.chat_answer_format_gate import SOURCE
from tools.chat_tool_recovery_gate import repair_action_ok


@pytest.mark.parametrize("kind,web,strict", [
    ("chat", True, False), ("chat", False, False), ("project", True, False),
    ("research", True, True), ("chat", True, True),
])
def test_web_file_guidance_belongs_only_to_ordinary_web_chat(kind: str, web: bool, strict: bool) -> None:
    policy = TaskPolicy(frozenset({"control", "web.read"}), strict_research=strict,
                        denied_capabilities=frozenset() if web else frozenset({"web.read"}))
    guidance = task_guidance_for_policy(policy, task_kind=kind)
    assert ("do not create local files" in guidance) == (kind == "chat" and web and not strict)
    assert ("The application generates URLs" in guidance) == (kind == "chat" and web and not strict)
    assert "Copy acquired source URLs exactly" not in guidance


@pytest.mark.parametrize("native", [False, True])
def test_invalid_file_call_can_be_abandoned_without_changing_its_frozen_schema(native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"})), task_kind="chat")
    snapshot = build_turn_snapshot(session, native=native)
    assert {"read_file", "web_search", "done"} <= set(snapshot.tool_names)
    args: ToolArgs = {"path": "D:/fictional/福州美食.md"}
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("bad-read", "read_file", args),)) if native else
             decode_text_turn(json.dumps({"tool": "read_file", "args": args})))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert plan.protocol_error and not plan.calls
    assert plan.protocol_tool_name == "read_file"
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native,
                            reply=reply if isinstance(reply, AssistantTurn) else None)
    assert "Reconsider whether this action is needed" in prompt
    assert "If unnecessary" in prompt and "declared completion contract" in prompt
    assert "If still needed, correct the rejected call" in prompt
    assert "Preserve argument strings exactly" in prompt
    assert "Correct read_file" not in prompt
    assert args == {"path": "D:/fictional/福州美食.md"}
    assert not session.executed
    spec = next(spec for spec in snapshot.model_specs if spec.name == "read_file")
    assert spec.required == ("path",)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("name", ["missing_tool", "shell"])
def test_unavailable_action_cannot_use_reconsideration_to_bypass_policy(native: bool, name: str) -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"}))), native=native)
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", name, {}),)), snapshot=snapshot)
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native)
    assert "Do not resend this unavailable tool or use another tool to bypass its restriction" in prompt
    assert "If unnecessary" not in prompt


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("research", [False, True])
def test_completion_errors_still_require_correcting_the_declared_submission(native: bool, research: bool) -> None:
    policy = TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}), strict_research=research,
                        required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS if research else ())
    session = TaskSession(policy, task_kind="research" if research else "chat",
                          research_questions=("What is supported?",) if research else ())
    snapshot = build_turn_snapshot(session, native=native)
    name = "submit_research_answer" if research else "done"
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", name, {}),)), snapshot=snapshot)
    assert plan.protocol_error and plan.protocol_tool_name == name
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native)
    assert "If unnecessary" not in prompt and "Correct the rejected call" in prompt
    if research:
        assert snapshot.research_scope is not None
        assert "submit_research_answer" in prompt


def test_missing_file_remains_an_execution_error_and_is_not_created(tmp_path: Path) -> None:
    result = read_file(tmp_path, "福州美食.md")
    assert not result.ok and "not a file: 福州美食.md" in result.model_text
    assert not (tmp_path / "福州美食.md").exists()


@pytest.mark.parametrize("native", [False, True])
def test_research_acquisition_error_does_not_offer_an_alternate_completion_contract(native: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.write"}),
                                    strict_research=True), task_kind="research")
    snapshot = build_turn_snapshot(session, native=native)
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("bad", "web_search", {}),)), snapshot=snapshot)
    assert plan.protocol_error and plan.protocol_tool_name == "web_search"
    prompt = _repair_prompt(plan, snapshot=snapshot, native=native)
    assert "If unnecessary" not in prompt and "Correct the rejected call" in prompt


def test_repair_probe_accepts_an_alternate_web_action_and_rejects_repeated_fictional_reads() -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read"}))))

    def proposal(name: str, args: ToolArgs) -> tuple[bool, bool]:
        plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("next", name, args),)), snapshot=snapshot)
        return repair_action_ok(plan, necessary=False), repair_action_ok(plan, necessary=True)

    assert proposal("web_search", {"query": "福州美食"}) == (True, False)
    assert proposal("open_url", {"url": SOURCE}) == (True, False)
    assert proposal("read_file", {"path": "福州美食.md"}) == (False, False)
    assert proposal("read_file", {"path": "notes.md"}) == (False, True)
    assert proposal("read_file", {"path": "D:/fictional/notes.md"}) == (False, False)
    assert proposal("done", {"summary": "鱼丸、肉燕和锅边糊。来源：" + SOURCE}) == (True, False)
