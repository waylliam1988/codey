"""Interpreter identity, durable approval and real process boundary regressions."""
from __future__ import annotations

import json
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.agents.shell_approval import ShellApprovalRequest, build_shell_approval_pending
from codey.app import shell_service
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from codey.operations.kernel_prompt import kernel_prompt_for_session
from codey.operations.task_session import TaskSession
from codey.policies.command_line import ShellSpec, resolve_shell
from codey.policies.task_policy import TaskPolicy
from codey.storage.command_permissions import CommandPermissionStore
from tests.support.shell_approval import admit_shell_task, seed_requested_approval


@pytest.mark.parametrize("platform", ["linux", "darwin", "cygwin"])
def test_posix_shell_ignores_terminal_preferences(platform):
    shell = resolve_shell(platform=platform, environ={"SHELL": "/bin/fish", "COMSPEC": "pwsh.exe"})
    assert shell == ShellSpec("posix", "/bin/sh")
    assert shell.invocation('printf "%s" "a b"') == ["/bin/sh", "-c", 'printf "%s" "a b"']


def test_windows_resolution_has_no_unknown_interpreter_fallback():
    assert resolve_shell(platform="win32", environ={"SystemRoot": r"D:\Windows"}) == ShellSpec(
        "cmd", r"D:\Windows\System32\cmd.exe")
    for path in ("cmd.exe", r"C:\Tools\pwsh.exe", r"C:cmd.exe"):
        with pytest.raises(ValueError):
            resolve_shell(platform="win32", environ={"COMSPEC": path})


@pytest.mark.parametrize("value", [None, {}, {"kind": [], "executable": "/bin/sh"},
                                  {"kind": "bash", "executable": "/bin/bash"},
                                  {"kind": "posix", "executable": "/tmp/sh"},
                                  {"kind": "posix", "executable": "/bin/sh", "args": ["-l"]}])
def test_invalid_or_old_approval_never_infers_an_interpreter(value):
    with pytest.raises(ValueError):
        ShellSpec.from_payload(value)


def test_session_prompt_and_approval_keep_the_original_shell(monkeypatch):
    shell = ShellSpec("cmd", r"C:\Old Windows\System32\cmd.exe")
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})), shell=shell)
    monkeypatch.setenv("COMSPEC", r"D:\Other\cmd.exe")
    prompt = kernel_prompt_for_session(session, tool_names=("run", "shell", "done"))
    assert shell.executable in prompt and r"D:\Other\cmd.exe" not in prompt
    assert "directly as argv, without a shell" in prompt
    assert "%NAME%" in prompt and "single quotes do not group" in prompt
    assert "Absolute paths and script filenames are not valid working directories" in prompt
    approval = ShellApprovalRequest(".", "echo hello", shell=session.shell)
    pending = build_shell_approval_pending(approval=approval, approval_id="a", session_id="s", run_id="r",
        project=".", attached_project=None, intent="chat", max_turns=2, provider_label="local")
    assert pending["shell"] == approval.to_payload()["shell"] == shell.to_payload()
    assert ShellSpec.from_payload(pending["shell"]).invocation('echo "a b"') == (
        '"C:\\Old Windows\\System32\\cmd.exe" /d /s /c "echo "a b""')


def test_grants_bind_the_persisted_interpreter(tmp_path):
    shell = resolve_shell()
    pending = {"project": str(tmp_path), "command": "git status", "session_id": "s", "shell": shell.to_payload()}
    store = CommandPermissionStore(tmp_path)
    store.remember(pending, "session")
    assert store.matching(pending)
    other = ShellSpec("cmd", r"D:\Other\cmd.exe") if os.name == "nt" else ShellSpec("cmd", r"C:\Windows\cmd.exe")
    assert not store.matching({**pending, "shell": other.to_payload()})
    assert not store.matching({key: value for key, value in pending.items() if key != "shell"})
    rules = json.loads(store.path.read_text(encoding="utf-8"))
    rules["rules"][0]["executor"] = shell.executable
    store.path.write_text(json.dumps(rules), encoding="utf-8")
    assert not store.matching(pending), "old grants must not acquire the new startup contract"


def test_approval_analysis_uses_the_same_pinned_dialect_as_the_prompt(tmp_path, monkeypatch):
    from codey.policies.command_approval import approval_call
    from codey.runtime.core.models import ToolCall

    call = ToolCall("shell", {"command": "python - <<'PY'\nprint(1)\nPY", "path": "."})
    monkeypatch.setenv("COMSPEC", "unsupported-shell.exe")
    assert approval_call(call, project=tmp_path, permission_profile="coding_writer",
                         shell=ShellSpec("posix", "/bin/sh")) is call
    assert approval_call(call, project=tmp_path, permission_profile="coding_writer",
                         shell=ShellSpec("cmd", r"C:\Windows\System32\cmd.exe")) is None


def test_command_path_guidance_appears_once_for_both_tools():
    from codey.policies.command_line import COMMAND_WORKDIR_DESCRIPTION
    session = TaskSession(policy=TaskPolicy(frozenset({"control"})))
    prompt = kernel_prompt_for_session(session, tool_names=("run", "shell", "done"))
    assert prompt.count(COMMAND_WORKDIR_DESCRIPTION) == 1


def test_restart_restores_the_approved_shell_without_resolving_again(tmp_path, monkeypatch):
    shell = resolve_shell()
    state = AppContext(tmp_path, start_scheduler=False)
    try:
        seed_requested_approval(state, continue_after=False)
    finally:
        state.close()
    monkeypatch.setenv("COMSPEC", r"D:\Changed\cmd.exe")
    restored = AppContext(tmp_path, start_scheduler=False)
    try:
        restore_pending_shell_approvals(restored)
        pending = restored.approvals.shell_snapshot()["approval"]
        assert pending["shell"] == shell.to_payload()
        execute = Mock(return_value={"ok": True, "status": "exit", "output": "ok", "exit_code": 0})
        monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
        status, _ = restored.shell_approval_workflow.accept("approval", True, Mock())
        assert status == 202 and restored.shell_approval_workflow.wait(5)
        assert execute.call_count == 1 and execute.call_args.args[1].shell == shell
    finally:
        restored.close()


def test_missing_shell_artifact_cannot_be_recovered_as_executable(tmp_path):
    from codey.runtime.effects.shell_approval_records import ShellApprovalFact
    state = AppContext(tmp_path, start_scheduler=False)
    try:
        admit_shell_task(state, session_id="s", run_id="r")
        artifact = state.managed_outputs.write_json_artifact(session_id="s", run_id="r",
            payload={"command": "echo old", "cwd": ".", "project": str(tmp_path)})
        state.record_shell_approval_fact(ShellApprovalFact("old", "s", "r", "requested",
            command_ref=artifact.handle, command_digest=artifact.sha256))
        restore_pending_shell_approvals(state)
        assert "old" not in state.approvals.shell_snapshot()
    finally:
        state.close()


def _context():
    return SimpleNamespace(_shell_spawn_gate=threading.Lock(), approval_generation=lambda: 0,
                           run_registry=SimpleNamespace(stop_flag=threading.Event()))


@pytest.mark.parametrize("exit_code", [0, 7])
def test_real_shell_keeps_quotes_unicode_cwd_and_exit_status(tmp_path, monkeypatch, exit_code):
    work = tmp_path / "directory with spaces"
    work.mkdir()
    script = work / "probe script.py"
    script.write_text("import json,os,sys\nprint(json.dumps([sys.argv[1],os.path.basename(os.getcwd()),'中文'],ensure_ascii=False))\nsys.exit(int(sys.argv[2]))", encoding="utf-8")
    shell = resolve_shell()
    if shell.kind == "cmd":
        command = f'"{sys.executable}" "probe script.py" "argument with spaces" {exit_code}'
    else:
        import shlex
        command = f'{shlex.quote(sys.executable)} "probe script.py" "argument with spaces" {exit_code}'
    ticket = shell_service.ShellExecutionTicket(command, work, 0, 10, 4000, shell=shell)
    monkeypatch.setenv("COMSPEC", r"D:\Does Not Exist\cmd.exe")
    result = shell_service.execute_shell_ticket(_context(), ticket)
    assert result["status"] == "exit" and result["exit_code"] == exit_code, result
    assert json.loads(result["output"]) == ["argument with spaces", work.name, "中文"]


@pytest.mark.skipif(os.name != "nt", reason="CMD quoting is a Windows process contract")
def test_cmd_preserves_raw_quotes_and_environment_assignment(tmp_path):
    command = 'set "CODEY_SHELL_PROBE=two words" && python -c "import os; print(os.environ[\'CODEY_SHELL_PROBE\'])"'
    ticket = shell_service.ShellExecutionTicket(command, tmp_path, 0, 10, 4000)
    result = shell_service.execute_shell_ticket(_context(), ticket)
    assert result["status"] == "exit" and result["exit_code"] == 0, result
    assert result["output"] == "two words"


@pytest.mark.skipif(os.name != "nt", reason="CMD command-tail quoting is Windows-specific")
def test_cmd_executes_a_quoted_batch_path_with_spaces(tmp_path):
    script = tmp_path / "batch with spaces.cmd"
    script.write_bytes(b"@echo off\r\necho [%~1]\r\nexit /b 7\r\n")
    ticket = shell_service.ShellExecutionTicket(f'"{script}" "two words"', tmp_path, 0, 10, 4000)
    result = shell_service.execute_shell_ticket(_context(), ticket)
    assert result["status"] == "exit" and result["exit_code"] == 7, result
    assert result["output"] == "[two words]"


def test_real_shell_timeout_is_not_a_successful_exit(tmp_path):
    ticket = shell_service.ShellExecutionTicket('python -c "import time; time.sleep(30)"', tmp_path, 0, 1, 4000)
    result = shell_service.execute_shell_ticket(_context(), ticket)
    assert not result["ok"] and result["status"] == "timeout" and result["exit_code"] is None, result


def test_spawn_error_never_switches_interpreters(tmp_path, monkeypatch):
    start = Mock(side_effect=FileNotFoundError("interpreter missing"))
    monkeypatch.setattr(shell_service.cancellation, "start_process", start)
    ticket = shell_service.ShellExecutionTicket("echo hello", tmp_path, 0, 10, 4000)
    result = shell_service.execute_shell_ticket(_context(), ticket)
    assert not result["ok"] and result["status"] == "spawn_error" and result["exit_code"] is None
    assert start.call_count == 1
    assert start.call_args.args == (ticket.shell.invocation(ticket.command),)
    assert start.call_args.kwargs.get("shell", False) is False
