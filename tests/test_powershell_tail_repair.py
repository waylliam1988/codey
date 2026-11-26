"""A single parser-proven tail edit precedes the existing approval contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from codey.env_names import SHELL_EXECUTABLE_ENV
from codey.operations.kernel.kernel_protocol import normalize_turn
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.command_approval import prepare_approval_call
from codey.policies.command_line import ShellSpec, resolve_shell
from codey.policies.shell_analysis import ShellAnalysis, analyze_shell, bound_analysis, prepare_shell_command
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall
from codey.runtime.effects.effect_records import compute_args_digest
from codey.types.json_value import JsonObject, parse_json_object, require_json_string
from tests.test_release_shell_continuation_http import model_peer, wait_for_completed_task
from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import GateHTTPClient, gate_application, probe_command, run_shell_case, wait_for_run


@pytest.fixture
def powershell(monkeypatch: pytest.MonkeyPatch) -> ShellSpec:
    try:
        shell = resolve_shell(environ={SHELL_EXECUTABLE_ENV: "pwsh"})
    except ValueError as exc:
        pytest.skip(f"PowerShell unavailable: {exc}")
    monkeypatch.setenv(SHELL_EXECUTABLE_ENV, shell.executable)
    return shell


@pytest.mark.parametrize("command", [
    'Write-Output "quoted"',
    'Write-Output hello`"',
    'Write-Output ok # "',
    'Write-Output "unfinished',
    'Write-Output "unfinished""',
    'Write-Output (1 + ); Write-Output ok"',
    '"',
])
def test_other_commands_remain_byte_identical(command: str, powershell: ShellSpec) -> None:
    effective, analysis = prepare_shell_command(command, powershell)
    assert effective == command
    assert analysis == analyze_shell(command, powershell)


@pytest.mark.parametrize("prefix,suffix", [("", ""), ("Write-Output '😀'; ", "")])
def test_parser_repairs_only_the_isolated_tail_token(prefix: str, suffix: str, powershell: ShellSpec) -> None:
    valid = prefix + probe_command("shell_failure", shell=powershell)
    original = valid + '"' + suffix
    assert analyze_shell(original, powershell).status == "invalid"
    effective, analysis = prepare_shell_command(original, powershell)
    assert effective == valid + suffix
    assert analysis == analyze_shell(effective, powershell)
    assert analysis.matches(effective, powershell) and not analysis.matches(original, powershell)
    # ExitStatementAst remains outside auto-grant syntax, even after repair.
    assert analysis.status == "opaque" and not analysis.reusable


@pytest.mark.parametrize("command", ["", "\0\"", "x" * 32768 + '"'], ids=["empty", "nul", "oversized"])
def test_input_limits_precede_the_parser(command: str, powershell: ShellSpec, monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("bounded input must not reach the parser")

    monkeypatch.setattr("codey.policies.shell_powershell.cancellation.run_process", unexpected)
    effective, analysis = prepare_shell_command(command, powershell)
    assert effective == command and analysis.status in {"invalid", "opaque"}


def test_parser_failure_keeps_the_original_command(powershell: ShellSpec, monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("parser", 5)

    monkeypatch.setattr("codey.policies.shell_powershell.cancellation.run_process", unavailable)
    original = 'Write-Output never_rewritten"'
    effective, analysis = prepare_shell_command(original, powershell)
    assert effective == original and analysis.matches(original, powershell)
    assert analysis.status == "opaque" and "unavailable" in analysis.reason


def test_approval_binds_its_existing_whitespace_normalization(tmp_path: Path, powershell: ShellSpec) -> None:
    prepared = prepare_approval_call(ToolCall("shell", {"command": 'Write-Output ok "', "path": "."}),
                                    project=tmp_path, permission_profile="coding_writer", shell=powershell)
    assert prepared is not None
    call, analysis = prepared
    assert call.args["command"] == "Write-Output ok"
    assert analysis.matches("Write-Output ok", powershell)


@pytest.mark.parametrize("native", [False, True])
def test_both_protocols_prepare_the_same_call_before_approval(tmp_path: Path, powershell: ShellSpec, native: bool) -> None:
    command = probe_command("shell_failure", shell=powershell)
    args: JsonObject = {"command": command + '"', "path": "."}
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control", "shell.approval"})), shell=powershell))
    reply = (AssistantTurn(tool_calls=(ProviderToolCall("original-id", "shell", args),)) if native else
             decode_text_turn(json.dumps({"tool": "shell", "args": args})))
    plan = normalize_turn(reply, snapshot=snapshot)
    assert not plan.protocol_error and len(plan.calls) == 1
    original = plan.calls[0]
    prepared = prepare_approval_call(original, project=tmp_path, permission_profile="coding_writer", shell=powershell)
    assert prepared is not None
    call, analysis = prepared
    assert call.args == {"command": command, "path": "."}
    assert call.call_id == original.call_id == ("original-id" if native else "")
    assert original.args["command"] == command + '"'
    assert analysis.matches(command, powershell)
    # Direct-argv run keeps its original validation and never adopts this repair.
    assert prepare_approval_call(ToolCall("run", dict(original.args)), project=tmp_path,
                                 permission_profile="coding_writer", shell=powershell) is None


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_repaired_command_uses_real_http_approval_and_original_result(
    tmp_path: Path, powershell: ShellSpec, protocol: str,
) -> None:
    case = "shell_failure"
    with model_peer(protocol, case, malformed_quote=True) as (url, requests, errors):
        target = GateTarget(url, "shell-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=30)
        proof = run_shell_case(case, target, tmp_path)
        assert not errors and len(requests) == 2
        assert proof["ok"] and proof["source_run_id"] == proof["continuation_run_id"]
        assert proof["stop_reason"] == "done"


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_repair_cannot_bypass_a_previous_denial(tmp_path: Path, powershell: ShellSpec, protocol: str) -> None:
    command = probe_command("shell_failure", shell=powershell)
    with model_peer(protocol, "shell_deny", retry_denied=True, malformed_quote=True, command=command) as (url, requests, errors):
        target = GateTarget(url, "shell-fixture", 32768, 8192, 12000, api_protocol=protocol, request_timeout=30)
        with gate_application(target, tmp_path) as (httpd, ctx, events):
            client = GateHTTPClient(httpd)
            submitted = client.post("/api/run", {
                "session_id": "tail-denied", "input": {"kind": "start_task", "text": "Run the shell probe"},
                "provider": "local", "model_selection": {"model": target.model, "base_url": url},
                "intent": "chat", "max_turns": 6,
            })
            run_id = require_json_string(submitted["run_id"], "run_id")
            assert wait_for_run(client, run_id, 30)["stop_reason"] == "approval"
            approval_id, pending = next(iter(ctx.pending_shell_approvals().items()))
            assert pending["command"] == command
            client.post("/api/shell_approval", {"id": approval_id, "approved": False})
            wait_for_completed_task(ctx, events, run_id, requests, errors)
            assert len(requests) == 3 and not errors and not ctx.pending_shell_approvals()
            assert not [effect for effect in ctx.durable.effects(run_id) if effect.category == "shell"]


def test_restart_uses_the_saved_prepared_command_without_repairing_again(
    tmp_path: Path, powershell: ShellSpec, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    before, after = tmp_path / "before", tmp_path / "after"
    for directory in (before, after):
        directory.mkdir()
        (directory / "input.json").write_text(json.dumps({"state": str(state_root)}), encoding="utf-8")
    with model_peer("openai-completions", "shell_failure", malformed_quote=True) as (url, requests, errors):
        target = GateTarget(url, "shell-fixture", 32768, 8192, 12000, request_timeout=30)
        # The normal gate cleanup sends Stop and cancels approvals. Crash an
        # actual preparation process instead, leaving its durable request intact.
        script = '''
import json, os, sys
from pathlib import Path
from tools.local_model_gate_attempts import GateTarget
from tools.local_model_gate_shell import GateHTTPClient, gate_application, wait_for_run
target = GateTarget(sys.argv[1], "shell-fixture", 32768, 8192, 12000, request_timeout=30)
directory = Path(sys.argv[2])
with gate_application(target, directory) as (httpd, ctx, events):
    client = GateHTTPClient(httpd)
    submitted = client.post("/api/run", {
        "session_id": "tail-restart", "input": {"kind": "start_task", "text": "Run the shell probe"},
        "provider": "local", "model_selection": {"model": target.model, "base_url": target.base_url},
        "intent": "chat", "max_turns": 6})
    assert wait_for_run(client, submitted["run_id"], 30)["stop_reason"] == "approval"
    approval_id, pending = next(iter(ctx.pending_shell_approvals().items()))
    (directory / "crash.json").write_text(json.dumps({
        "run_id": submitted["run_id"], "approval_id": approval_id,
        "analysis": pending["analysis"], "command": pending["command"]}), encoding="utf-8")
    os._exit(75)
'''
        child = subprocess.run([sys.executable, "-c", script, url, str(before)],
                               capture_output=True, text=True, timeout=60)
        assert child.returncode == 75, child.stderr
        saved = parse_json_object((before / "crash.json").read_text(encoding="utf-8"))
        run_id = require_json_string(saved["run_id"], "run_id")
        approval_id = require_json_string(saved["approval_id"], "approval_id")
        command = probe_command("shell_failure", shell=powershell)
        assert saved["command"] == command
        analysis = saved["analysis"]

        def unexpected(*args: object, **kwargs: object) -> object:
            raise AssertionError("restored approval must not analyze or prepare again")

        monkeypatch.setattr("codey.policies.shell_powershell.inspect_powershell", unexpected)
        with gate_application(target, after) as (httpd, ctx, events):
            client = GateHTTPClient(httpd)
            restored = ctx.pending_shell_approvals()[approval_id]
            assert restored["command"] == command
            assert bound_analysis(command, powershell, restored["analysis"]) == ShellAnalysis.from_payload(analysis)
            assert restored["tool_args"] == {"command": command, "path": "."}
            assert restored["call_id"] == "original-shell-call"
            intents = [effect for effect in ctx.durable.effects(run_id) if effect.category == "tool_call"]
            assert len(intents) == 1
            assert intents[0].intent["args_digest"] == compute_args_digest({"command": command, "path": "."})
            client.post("/api/shell_approval", {"id": approval_id, "approved": True})
            wait_for_completed_task(ctx, events, run_id, requests, errors)
            shell_effects = [effect for effect in ctx.durable.effects(run_id) if effect.category == "shell"]
            assert len(shell_effects) == 1 and shell_effects[0].status != "pending"
            assert len(requests) == 2 and not errors
            results = [event for event in events if event.get("type") == "shell_result"]
            assert len(results) == 1 and results[0]["exit_code"] == 7
