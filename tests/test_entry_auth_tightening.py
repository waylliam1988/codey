"""derive_entry_auth：权限回答可以做什么，完成要求回答必须做什么。"""
from __future__ import annotations

from codey.app.api import derive_entry_auth


def test_negation_does_not_kill_network_grant_in_other_clause() -> None:
    auth = derive_entry_auth({"intent": "auto", "task": "不要修改代码，请查官方文档"})
    assert "web.read" in auth.requested_capabilities, f"否定只作用对应动作，不应影响另一分句：{auth}"


def test_auto_permission_does_not_require_a_local_lookup_to_use_web() -> None:
    auth = derive_entry_auth({"intent": "auto", "task": "查一下本地函数定义"})
    assert "web.read" in auth.requested_capabilities
    assert not auth.sources_open_required


def test_allow_web_does_not_force_open_requirement() -> None:
    auth = derive_entry_auth({"intent": "auto", "task": "检查本地代码", "allow_web": True})
    assert "web.read" in auth.requested_capabilities
    # 许可与完成义务独立，不从任务文字反推必须打开来源。
    assert not auth.sources_open_required, f"明确只读本地不应强制打开来源：{auth}"


def test_readonly_project_task_has_no_must_change() -> None:
    auth = derive_entry_auth({"intent": "project", "task": "检查 bug，不要修改任何文件", "project": "/tmp/proj"})
    assert not auth.project_changes_required, f"明确只读目标不能生成必须修改要求：{auth}"
