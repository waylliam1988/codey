"""项目审计顾问必须走统一模型工具循环（只读子会话），不再自建循环。

- 同一个 run_task_kernel：同快照、同解析、同执行、同 done 门；
- 父授权的子集：只读工具可见，edit/run/shell 拒绝且不落盘；
- 审计结果投影回调用方（返回 done 文本）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from codey.policies.task_policy import TaskPolicy


def _audit_policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"control", "project.read"}))


def test_audit_parent_without_project_read_never_scans_or_sends(tmp_path):
    from unittest.mock import patch

    from codey.operations.project_audit_advisor import run_project_audit_advisor
    from codey.policies.task_policy import TaskPolicy

    provider = _AuditProvider([])
    with patch("codey.operations.project_audit_advisor.visible_entries") as scan:
        assert run_project_audit_advisor(provider, tmp_path, "audit",
                                        parent_policy=TaskPolicy(grants=frozenset({"control"}))) == ""
    scan.assert_not_called()
    assert not provider.prompts


def test_audit_send_respects_remaining_time_budget(tmp_path):
    from codey.operations.project_audit_advisor import PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT, run_project_audit_advisor

    deadlines = []

    class Provider:
        def send(self, prompt, timeout=None):
            deadlines.append(timeout)
            return _done_call("clean")

    assert run_project_audit_advisor(Provider(), tmp_path, "audit", parent_policy=_audit_policy()) == "clean"
    assert deadlines
    assert all(value is not None and 0 < value <= PROJECT_AUDIT_ADVISOR_TOTAL_TIMEOUT for value in deadlines), deadlines


class _AuditProvider:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def send(self, prompt: str, timeout=None) -> str:
        self.prompts.append(str(prompt))
        return self._replies.pop(0)


def _read_call() -> str:
    return '{"tool": "read_file", "args": {"path": "app.py"}}'


def _done_call(summary: str) -> str:
    import json

    return json.dumps({"tool": "done", "args": {"summary": summary}})


def test_audit_advisor_reads_and_returns_done_via_shared_kernel():
    from unittest import mock

    from codey.operations.project_audit_advisor import run_project_audit_advisor

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td)
        (proj / "app.py").write_text("print('audit me')\n", encoding="utf-8")
        provider = _AuditProvider([_read_call(), _done_call("all clean")])
        with mock.patch(
            "codey.operations.kernel_transport.provider_uses_native", return_value=False,
        ):
            report = run_project_audit_advisor(provider, proj, "audit this code", parent_policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
        assert report == "all clean"
        assert "print('audit me')" in provider.prompts[1], "读结果必须进入下一轮提示"
        first_prompt = provider.prompts[0]
        assert "read_file" in first_prompt or "read-only" in first_prompt.lower()


def test_advisor_drives_single_shared_kernel_with_readonly_policy():
    """结构锁定：顾问必须调用统一 run_task_kernel，不得自建解析/执行循环。"""
    from unittest import mock

    from codey.operations import project_audit_advisor
    from codey.operations.task_loop import KernelResult

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td)
        (proj / "app.py").write_text("x\n", encoding="utf-8")
        provider = _AuditProvider([])
        with mock.patch(
            "codey.operations.project_audit_advisor.run_task_kernel",
            return_value=KernelResult(True, "kernel advice", 2, "done"),
        ) as kernel:
            report = project_audit_advisor.run_project_audit_advisor(provider, proj, "audit", parent_policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
        assert report == "kernel advice"
        assert kernel.call_count == 1
        session = kernel.call_args.args[0] if kernel.call_args.args else kernel.call_args.kwargs["session"]
        policy = getattr(session, "policy", None)
        assert policy is not None
        assert policy.allows("project.read") is True
        assert policy.allows("project.write") is False
        assert policy.allows("shell.approval") is False
        executors = kernel.call_args.kwargs["request"].execution.executors
        assert set(executors) == {"list_dir", "read_file", "grep", "find_references"}, (
            f"审计子会话只允许只读执行器：{sorted(executors)}"
        )


def test_audit_advisor_denies_write_without_side_effects():
    from unittest import mock

    from codey.operations.project_audit_advisor import run_project_audit_advisor

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        proj = Path(td)
        (proj / "app.py").write_text("original\n", encoding="utf-8")
        edit_call = (
            '{"tool": "edit", "args": {"path": "app.py", '
            '"replacements": [{"old_string": "original", "new_string": "hacked"}]}}'
        )
        provider = _AuditProvider([edit_call, _done_call("no changes needed")])
        with mock.patch(
            "codey.operations.kernel_transport.provider_uses_native", return_value=False,
        ):
            report = run_project_audit_advisor(provider, proj, "audit this code", parent_policy=TaskPolicy(grants=frozenset({"control", "project.read"})))
        assert report == "no changes needed"
        assert (proj / "app.py").read_text(encoding="utf-8") == "original\n"
        assert len(provider.prompts) >= 2, "拒绝后必须给模型修复提示"
