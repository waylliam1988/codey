"""明确只读 project 任务必须拒绝写权限（入口→策略→快照→执行）。

红测覆盖真实提交入口：derive_entry_auth → TaskSubmission →
build_task_policy → 快照可见性 → normalize_turn 强行调用被拒绝。
只断言 project_changes_required=False 是不够的。
"""

from __future__ import annotations

from types import SimpleNamespace

from codey.app.api import derive_entry_auth
from codey.operations.kernel_protocol import normalize_turn
from codey.policies.task_policy import TaskPolicy, build_task_policy
from codey.toolchain.tool_spec import visible_tool_names_for_snapshot


def _submission_from_entry(entry) -> SimpleNamespace:
    return SimpleNamespace(
        project="E:\\codey",
        task="只检查代码，不要修改文件",
        requested_capabilities=tuple(entry.requested_capabilities),
        strict_research=entry.strict_research,
        sources_open_required=entry.sources_open_required,
        project_changes_required=entry.project_changes_required,
        denied_capabilities=tuple(getattr(entry, "denied_capabilities", ())),
    )


def test_explicit_readonly_entry_denies_write_and_shell_surface():
    entry = derive_entry_auth(
        {"intent": "project", "task": "只检查代码，不要修改文件"},
        project="E:\\codey",
    )
    assert entry.project_changes_required is False
    denied = set(getattr(entry, "denied_capabilities", ()))
    assert "project.write" in denied
    assert "shell.approval" in denied
    assert "project.write" not in set(entry.requested_capabilities)


def test_readonly_policy_hides_edit_and_denies_forced_call():
    entry = derive_entry_auth(
        {"intent": "project", "task": "只检查代码，不要修改文件"},
        project="E:\\codey",
    )
    policy = build_task_policy(
        _submission_from_entry(entry), task_kind="project",
        strict_research=entry.strict_research,
    )
    assert policy.allows("project.write") is False
    assert policy.allows("shell.approval") is False
    assert policy.allows("project.read") is True
    names = visible_tool_names_for_snapshot(policy, None)
    assert "edit" not in names
    assert "read_file" in names
    plan = normalize_turn(
        '{"tool": "edit", "args": {"path": "a.py", "edits": []}}',
        policy=policy,
    )
    assert getattr(plan, "protocol_error", "") != ""
    assert plan.calls == []


def test_task_policy_rejects_unknown_version():
    import pytest

    with pytest.raises(ValueError):
        TaskPolicy.from_payload({"grants": ["project.write"], "version": 999})


def test_task_policy_rejects_malformed_versions_instead_of_guessing_current():
    import pytest

    for version in ("invalid", "1", True, 0, None, 1.0):
        with pytest.raises(ValueError):
            TaskPolicy.from_payload({"grants": ["project.write"], "version": version})


def test_task_policy_freezes_caller_owned_collections():
    grants = {"control"}
    checks = ["required"]
    policy = TaskPolicy(grants=grants, required_checks=checks)
    grants.add("project.write")
    checks.clear()
    assert not policy.allows("project.write")
    assert policy.required_checks == ("required",)


def test_build_policy_never_widens_explicit_denial():
    sub = SimpleNamespace(
        project="E:\\codey",
        requested_capabilities=("project.write", "shell.approval"),
        strict_research=False,
        sources_open_required=False,
        project_changes_required=False,
        denied_capabilities=("project.write", "shell.approval"),
    )
    policy = build_task_policy(sub, task_kind="project")
    assert policy.allows("project.write") is False
    assert policy.allows("shell.approval") is False


def test_recovered_policy_rejects_malformed_fields_instead_of_dropping_restrictions():
    import pytest

    good = TaskPolicy(grants=frozenset({"control", "project.write"})).to_payload()
    for field, value in (
        ("denied_capabilities", "project.write"),
        ("strict_research", "true"),
        ("sources_open_required", 1),
        ("required_checks", "research_sources_opened"),
        ("required_checks", [False]),
        ("grants", ["project.write", "unknown.permission"]),
    ):
        with pytest.raises(ValueError, match=field):
            TaskPolicy.from_payload({**good, field: value})


def test_execution_hint_preserves_explicit_denial():
    from types import SimpleNamespace

    from codey.operations.task_entry import build_task_policy_for_entry

    request = SimpleNamespace(project="", requested_capabilities=("web.read",),
                              denied_capabilities=("web.read",), model_hint="ACTION: research\nPLAN: read docs")
    policy = build_task_policy_for_entry(request, "project")
    assert not policy.allows("web.read")
    assert policy.denied_capabilities == frozenset({"web.read"})
