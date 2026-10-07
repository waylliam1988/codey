"""Task guidance belongs to task composition, not to the shared prompt renderer."""
import ast
import hashlib
import inspect
from itertools import product

import pytest

from codey.operations import kernel_prompt, task_loop
from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, tools_from_specs


def _guidance(policy):
    from codey.operations.task_guidance import task_guidance_for_policy

    return task_guidance_for_policy(policy)


def test_full_research_guidance_preserves_existing_prompt_bytes():
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        strict_research=True)
    assert hashlib.sha256(_guidance(policy).encode()).hexdigest() == (
        "a12196236980017040833f30e7c39dbe6932eeb4f867c5c2d668d7e2cac453a4"
    )


@pytest.mark.parametrize("native", [False, True])
def test_shared_kernel_sends_arbitrary_task_guidance_in_both_protocols(native, monkeypatch):
    from codey.env_names import NATIVE_TOOLS_ENV

    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1" if native else "0")
    sent = []

    class Provider:
        def send(self, text):
            sent.append(text)
            return "not a tool call"

        def send_turn(self, text, tools):
            sent.append(text)
            return AssistantTurn(text="not a tool call")

        def send_tool_results(self, messages, tools):
            raise AssertionError("no native tool calls were issued")

        def acknowledge_tool_results(self, results, declared_tools, timeout=None):
            return self.send_tool_results(results, [])

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})),
                          task_kind="third_task", max_turns=1)
    task_loop.run_task_kernel(session, request=KernelRunRequest(
        transport=KernelTransportDeps(provider=Provider(), provider_id="local"),
        task_guidance="Third task completion: preserve the supplied units.",
    ))
    assert len(sent) == 1
    assert "Third task completion: preserve the supplied units." in sent[0]
    assert "Research evidence rules" not in sent[0]


def test_ordinary_web_task_has_no_strict_research_guidance():
    policy = TaskPolicy(grants=frozenset({"control", "project.read", "web.read"}))
    assert _guidance(policy) == ""


@pytest.mark.parametrize("grants, forbidden", [
    ({"control"}, ("web_search", "open_result", "open_url", "knowledge_write", "knowledge_read")),
    ({"control", "web.read"}, ("knowledge_write", "knowledge_read")),
    ({"control", "web.read", "knowledge.write"}, ("knowledge_read",)),
    ({"control", "knowledge.read"}, ("web_search", "open_result", "open_url", "knowledge_write")),
])
def test_research_guidance_does_not_direct_unauthorized_tools(grants, forbidden):
    text = _guidance(TaskPolicy(grants=frozenset(grants), strict_research=True))
    assert "Report format is strict" in text
    for name in forbidden:
        assert name not in text


def test_research_guidance_respects_explicit_capability_denial():
    policy = TaskPolicy(grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
                        denied_capabilities=frozenset({"knowledge.write"}), strict_research=True)
    assert "knowledge_write" not in _guidance(policy)


def test_all_guidance_grant_and_denial_combinations_remain_within_authorization():
    capabilities = ("web.read", "knowledge.read", "knowledge.write")
    tool_capabilities = {"web_search": "web.read", "open_result": "web.read", "open_url": "web.read",
                         "knowledge_read": "knowledge.read", "knowledge_write": "knowledge.write"}
    for flags in product((False, True), repeat=6):
        policy = TaskPolicy(
            grants=frozenset({"control", *(cap for cap, enabled in zip(capabilities, flags[:3], strict=True) if enabled)}),
            denied_capabilities=frozenset(cap for cap, denied in zip(capabilities, flags[3:], strict=True) if denied),
            strict_research=True,
        )
        text = _guidance(policy)
        for tool, capability in tool_capabilities.items():
            if tool in text:
                assert policy.allows(capability), (flags, tool)


@pytest.mark.parametrize("native", [False, True])
def test_note_id_usage_is_owned_by_the_shared_tool_contract(native):
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.research.tool_contract import knowledge_read_id_guidance

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "knowledge.read"})))
    snapshot = build_turn_snapshot(session, native=native)
    if native:
        descriptions = {row.name: row.description for row in tools_from_specs(snapshot.frozen_specs)}
        assert knowledge_read_id_guidance() in descriptions["knowledge_read"]
    else:
        assert knowledge_read_id_guidance() in snapshot.contract_text


@pytest.mark.parametrize("native", [False, True])
def test_note_reader_description_does_not_advertise_a_hidden_write_tool(native):
    from codey.operations.kernel_protocol import build_turn_snapshot

    session = TaskSession(policy=TaskPolicy(
        grants=frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}), strict_research=True,
    ), task_kind="research")
    snapshot = build_turn_snapshot(session, native=native)
    assert "knowledge_read" in snapshot.tool_names
    assert "knowledge_write" not in snapshot.tool_names
    if native:
        description = next(row.description for row in tools_from_specs(snapshot.frozen_specs)
                           if row.name == "knowledge_read")
    else:
        description = snapshot.contract_text
    assert "knowledge_write" not in description
    assert "do not prepend facts/ or append .md" in description


def test_report_headings_come_from_the_existing_report_contract(monkeypatch):
    from codey.reviews import report_sections

    monkeypatch.setattr(report_sections, "REQUIRED_SECTIONS", ("conclusion", "sources"))
    text = _guidance(TaskPolicy(grants=frozenset({"control"}), strict_research=True))
    assert "## 结论, ## 来源." in text
    assert "## 关键证据" not in text


def test_kernel_prompt_has_no_domain_branch_or_completion_dependencies():
    source = inspect.getsource(kernel_prompt)
    assert "strict_research" not in source
    assert "Research evidence rules" not in source
    tree = ast.parse(source)
    forbidden = ("codey.research", "codey.operations.project_completion_checks",
                 "codey.operations.project_verification")
    imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert not any(name.startswith(forbidden) for name in imports)
