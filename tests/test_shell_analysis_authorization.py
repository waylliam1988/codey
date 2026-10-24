"""The committed analysis, not raw substrings or a recovery reparse, admits grants."""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from codey.agents.shell_approval import ShellApprovalRequest
from codey.app.context import AppContext
from codey.app.shell_approval_state import restore_pending_shell_approvals
from codey.app.shell_service import execute_shell_ticket
from codey.policies.action import ActionSubject, evaluate_action
from codey.policies.shell_analysis import analyze_shell
from codey.storage.command_permissions import CommandPermissionStore, project_rule_available
from tests.support.shell_approval import seed_requested_approval
from tests.test_shell_contract import _context, make_ticket
from tests.test_shell_interpreters import NAMES, installed


@pytest.mark.parametrize("name", NAMES)
def test_quoted_separators_can_be_saved_but_expansion_cannot(name, tmp_path):
    shell = installed(name)
    command = 'git log "--format=a;b|c"'
    analysis = analyze_shell(command, shell)
    assert project_rule_available(command, shell=shell, analysis=analysis), analysis
    request = ShellApprovalRequest(".", command, shell=shell, analysis=analysis)
    pending = {**request.to_payload(), "project": str(tmp_path), "session_id": "s"}
    store = CommandPermissionStore(tmp_path)
    store.remember(pending, "project")
    assert store.matching(pending)
    expanded = {'cmd': 'git status %PATH%', 'powershell': 'git status $env:PATH'}.get(shell.kind, 'git status "$PATH"')
    changing = {**ShellApprovalRequest(".", expanded, shell=shell).to_payload(), "project": str(tmp_path), "session_id": "s"}
    assert not store.matching(changing)
    with pytest.raises(ValueError, match="one-time"):
        store.remember(changing, "session")


@pytest.mark.parametrize("name", ["bash", "pwsh", "powershell"])
def test_login_and_dynamic_scripts_cannot_gain_reusable_grants(name, tmp_path):
    shell = installed(name)
    login = replace(shell, startup="login")
    for command, spec in (("git status", login),
                          ("Invoke-Expression $env:TARGET" if shell.kind == "powershell" else 'eval "$TARGET"', shell)):
        request = ShellApprovalRequest(".", command, shell=spec)
        pending = {**request.to_payload(), "project": str(tmp_path), "session_id": "s"}
        with pytest.raises(ValueError, match="one-time"):
            CommandPermissionStore(tmp_path).remember(pending, "session")


def test_mismatched_or_missing_analysis_is_denied_even_with_approval_channel(tmp_path):
    shell = installed("sh")
    analysis = analyze_shell("git status", shell)
    for value in (None, analysis):
        decision = evaluate_action(ActionSubject("shell", project=str(tmp_path), path=".",
            permission_profile="coding_writer", command="echo changed", shell=shell, analysis=value, approval_available=True))
        assert decision.decision == "deny"
        assert decision.reason_code in {"shell_analysis_required", "shell_analysis_mismatch"}


def test_execution_never_uses_analysis_for_a_different_command(tmp_path, monkeypatch):
    from codey.app import shell_service
    ticket = replace(make_ticket("echo approved", tmp_path, 0, 10, 4000), command="echo changed")
    start = Mock()
    monkeypatch.setattr(shell_service.cancellation, "start_process", start)
    result = execute_shell_ticket(_context(), ticket)
    assert not result["ok"] and result["status"] == "spawn_error" and not start.called


def test_recovery_reuses_saved_analysis_without_running_any_parser(tmp_path, monkeypatch):
    from codey.app import shell_service
    state = AppContext(tmp_path, start_scheduler=False)
    try:
        seed_requested_approval(state, continue_after=False)
    finally:
        state.close()
    parse = Mock(side_effect=AssertionError("recovery must not reparse"))
    monkeypatch.setattr("codey.policies.shell_analysis.analyze_shell", parse)
    monkeypatch.setattr("codey.policies.shell_powershell.cancellation.run_process", parse)
    restarted = AppContext(tmp_path, start_scheduler=False)
    try:
        restore_pending_shell_approvals(restarted)
        saved = restarted.approvals.shell_snapshot()["approval"]["analysis"]
        execute = Mock(return_value={"ok": True, "status": "exit", "exit_code": 0, "output": "done"})
        monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
        code, _ = restarted.shell_approval_workflow.accept("approval", True, Mock())
        assert code == 202 and restarted.shell_approval_workflow.wait(5)
        assert execute.call_count == 1 and execute.call_args.args[1].analysis.to_payload() == saved
        assert not parse.called
    finally:
        restarted.close()


def test_opaque_request_requires_once_but_can_still_be_explicitly_approved(tmp_path, monkeypatch):
    from codey.app import shell_service
    # CMD caret, POSIX control flow: both are outside the auto-grant subset.
    shell = installed("cmd" if __import__("os").name == "nt" else "sh")
    command = "echo ^literal" if shell.kind == "cmd" else "for x in a; do echo $x; done"
    state = AppContext(tmp_path, start_scheduler=False)
    try:
        seed_requested_approval(state, command=command, continue_after=False)
        restore_pending_shell_approvals(state)
        card = state.approvals.shell_snapshot()["approval"]["ui_event"]
        assert card["session_rule_available"] is False and card["project_rule_available"] is False
        execute = Mock(return_value={"ok": True, "status": "exit", "exit_code": 0, "output": "done"})
        monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
        for scope in ("session", "project"):
            code, _ = state.shell_approval_workflow.accept("approval", True, Mock(), scope=scope)
            assert code == 400 and not execute.called
            assert state.shell_approval_workflow.status("approval")["decision"] == "pending"
        code, _ = state.shell_approval_workflow.accept("approval", True, Mock(), scope="once")
        assert code == 202 and state.shell_approval_workflow.wait(5) and execute.call_count == 1
    finally:
        state.close()


@pytest.mark.parametrize("name", ["bash", "sh", "pwsh", "powershell"])
def test_wildcards_and_expanding_redirections_need_once(name, tmp_path):
    shell = installed(name)
    commands = ("Get-Content '*.txt'", "echo hi > *.txt") if shell.kind == "powershell" else (
        "cat *.txt", "echo hi > *.txt")
    store = CommandPermissionStore(tmp_path)
    for command in commands:
        request = ShellApprovalRequest(".", command, shell=shell)
        assert request.analysis.expansions and not request.analysis.reusable
        pending = {**request.to_payload(), "project": str(tmp_path), "session_id": "s"}
        with pytest.raises(ValueError, match="one-time"):
            store.remember(pending, "session")
