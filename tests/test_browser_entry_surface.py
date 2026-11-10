"""Explicit browser capabilities compose with existing frozen tool surfaces."""
from __future__ import annotations

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.task.entry_auth import derive_entry_auth
from codey.task.model import TaskSubmission

BROWSER = ("browser.observe", "browser.interact", "browser.effect")


@pytest.mark.parametrize("kind", ["chat", "project", "hybrid", "research"])
def test_browser_capabilities_survive_entry_policy_and_recovery_without_changing_answer_tools(kind: str) -> None:
    from codey.research.answer_contract import ANSWER_TOOLS

    auth = derive_entry_auth({"intent": kind, "requested_capabilities": BROWSER})
    request = TaskSubmission("s", None, "browser task", 8, False, "local",
                             requested_capabilities=auth.requested_capabilities,
                             strict_research=auth.strict_research)
    policy = build_task_policy(request, task_kind=kind)
    assert all(policy.allows(grant) for grant in BROWSER)
    assert TaskPolicy.from_payload(policy.to_payload()) == policy
    snapshot = build_turn_snapshot(TaskSession(policy=policy, task_kind=kind))
    assert "browser_eval" in snapshot.nested_tool_names
    assert not any(name.startswith("browser_") for name in snapshot.tool_names)
    assert not any(name.startswith("browser_") for name in ANSWER_TOOLS)
    assert set(snapshot.nested_tool_names).isdisjoint(snapshot.tool_names)
    if kind == "research":
        assert "submit_research_answer" in snapshot.tool_names
        assert "done" not in snapshot.tool_names


def test_browser_authority_is_explicit_and_network_off_is_final() -> None:
    assert not set(derive_entry_auth({"intent": "chat", "task": "打开网页，点击发送邮件"}).requested_capabilities) & set(BROWSER)
    auth = derive_entry_auth({"intent": "chat", "allow_web": False, "requested_capabilities": BROWSER})
    request = TaskSubmission("s", None, "task", 8, False, "local",
                             requested_capabilities=BROWSER, denied_capabilities=auth.denied_capabilities)
    assert not any(build_task_policy(request, task_kind="chat").allows(grant) for grant in BROWSER)


def test_planning_never_gains_browser_mutations() -> None:
    request = TaskSubmission("s", None, "task", 8, False, "local", requested_capabilities=BROWSER)
    policy = build_task_policy(request, task_kind="planning")
    assert policy.allows("browser.observe")
    assert not policy.allows("browser.interact")
    assert not policy.allows("browser.effect")


def test_outer_protocol_rejects_hidden_browser_leaf_and_keeps_one_frozen_catalog() -> None:
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "browser.observe", "browser.interact"})),
                          task_kind="chat")
    snapshot = build_turn_snapshot(session)
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("c1", "browser_click", {"ref": "forged"}),)),
                          snapshot=snapshot)
    assert plan.protocol_error
    assert "browser_eval" not in snapshot.inventory_names
    codemode = next(spec for spec in snapshot.frozen_specs if spec.name == "codemode")
    assert codemode.declarations.count("browser_snapshot(args:") == 1
    assert {spec.name for spec in snapshot.frozen_specs} == set(snapshot.inventory_names)
