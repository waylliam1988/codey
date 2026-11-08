"""Entry defaults grant tools; task text never grants or forces acquisition."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from codey.app.api import run_submit_response
from codey.policies.task_policy import TaskPolicy
from codey.task.entry_auth import derive_entry_auth


@pytest.mark.parametrize("task", ["你好", "查一下本地函数", "搜索一下附近有什么好玩的", "search nearby parks"])
@pytest.mark.parametrize("intent", ["chat", "auto"])
def test_chat_default_permission_is_independent_of_words(task: str, intent: str) -> None:
    auth = derive_entry_auth({"intent": intent, "task": task})
    assert "web.read" in auth.requested_capabilities
    assert not auth.sources_open_required


@pytest.mark.parametrize("intent", ["chat", "auto", "project", "research"])
def test_explicit_off_wins_over_requested_permission(intent: str) -> None:
    auth = derive_entry_auth({"intent": intent, "task": "搜索一下", "allow_web": False,
                              "requested_capabilities": ["web.read"]})
    assert "web.read" not in auth.requested_capabilities
    assert "web.read" in auth.denied_capabilities


def test_project_permission_needs_request_instead_of_words() -> None:
    auth = derive_entry_auth({"intent": "project", "task": "上网搜索官方文档"})
    assert "web.read" not in auth.requested_capabilities


@pytest.mark.parametrize("allow_web", [None, False, True])
def test_http_passes_the_same_effective_entry_policy(allow_web: bool | None) -> None:
    submit = Mock(return_value="run_" + "a" * 32)
    body: dict[str, object] = {"intent": "chat", "task": "东百中心附近有什么好玩的"}
    if allow_web is not None:
        body["allow_web"] = allow_web
    status, _ = run_submit_response(body, submit)
    assert status == 200
    auth = derive_entry_auth(body)
    for field in ("requested_capabilities", "denied_capabilities", "sources_open_required"):
        assert submit.call_args.kwargs[field] == getattr(auth, field)


def test_restored_policy_does_not_apply_new_defaults() -> None:
    frozen = TaskPolicy(grants=frozenset({"control", "knowledge.read"}), source="historical")
    restored = TaskPolicy.from_payload(frozen.to_payload())
    assert restored == frozen
    assert not restored.allows("web.read")
    assert "web.read" in derive_entry_auth({"intent": "chat"}).requested_capabilities
