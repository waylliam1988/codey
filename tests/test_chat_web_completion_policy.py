"""Web permission and optional use do not create additional completion duties."""
from __future__ import annotations

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult


@pytest.mark.parametrize("lookup", ["none", "official_link", "optional_failure"])
def test_chat_can_finish_without_global_page_open_requirement(lookup: str) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat")
    if lookup == "official_link":
        session.search_results["r1"] = "https://example.com/"
        session._memory_results["search"] = ToolResult(ToolCall("web_search", {"query": "official website"}),
            "Official website https://example.com/", ok=True)
        answer = "官网链接：https://example.com/"
    elif lookup == "optional_failure":
        session._memory_results["fetch"] = ToolResult(ToolCall("open_url", {"url": "https://example.com/"}),
            "ERROR: page_blank", ok=False,
            canonical={"source_failure": {"url": "https://example.com/", "detail": "page_blank"}})
        answer = "可先参考已知的行程安排；最新营业时间未能验证。"
    else:
        answer = "你好！"
    verdict = evaluate(session, answer)
    assert verdict.complete and not verdict.blocked
    assert not session.opened_sources


def test_web_evidence_guidance_is_scoped_to_authorized_ordinary_chat() -> None:
    policy = TaskPolicy(frozenset({"control", "web.read"}))
    chat = task_guidance_for_policy(policy, task_kind="chat")
    assert "explicit requests to search" in chat and "Search titles and snippets" in chat
    assert "official link" in chat and "remains unverified" in chat
    assert "Web reading:" not in task_guidance_for_policy(policy, task_kind="project")
    assert "Web reading:" not in task_guidance_for_policy(TaskPolicy(frozenset({"control"})), task_kind="chat")
