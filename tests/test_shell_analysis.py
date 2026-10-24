"""Native syntax trees, literal/expanding words and no execution during analysis."""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace

import pytest

from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis, analyze_shell
from tests.test_shell_interpreters import NAMES, installed


@pytest.mark.parametrize("name", NAMES)
def test_simple_literals_share_one_analysis_contract(name):
    shell = installed(name)
    analysis = analyze_shell('git status --short', shell)
    assert analysis.status == "parsed", analysis
    assert analysis.simple_argv == ("git", "status", "--short")
    assert analysis.reusable and not analysis.dynamic
    assert analysis.to_payload()["command_sha256"] == analysis.command_sha256


@pytest.mark.parametrize("name", NAMES)
def test_pipeline_redirection_and_expansion_are_distinct(name):
    shell = installed(name)
    command = {'cmd': 'echo %PATH% | findstr x > out.txt',
               'powershell': 'Write-Output $env:PATH | Select-String x > out.txt'}.get(
                   shell.kind, 'echo "$PATH" | cat > out.txt')
    analysis = analyze_shell(command, shell)
    assert analysis.status == "parsed", analysis
    assert len(analysis.commands) == 2
    assert "|" in analysis.operators and analysis.redirections and analysis.expansions
    assert analysis.simple_argv is None and not analysis.reusable


@pytest.mark.parametrize("name", NAMES)
def test_quoted_metacharacters_remain_literal(name):
    shell = installed(name)
    command = 'echo "a | b > c"' if shell.kind == "cmd" else "echo 'a | b > c'"
    analysis = analyze_shell(command, shell)
    assert analysis.status == "parsed", analysis
    assert analysis.simple_argv == ("echo", "a | b > c")
    assert not analysis.operators and not analysis.redirections


@pytest.mark.parametrize("name", NAMES)
def test_dynamic_execution_is_never_a_simple_literal_command(name):
    shell = installed(name)
    command = {'cmd': 'call echo %TARGET%', 'powershell': '& $env:TARGET'}.get(shell.kind, 'eval "$TARGET"')
    analysis = analyze_shell(command, shell)
    assert analysis.dynamic and not analysis.reusable and analysis.simple_argv is None, analysis


@pytest.mark.parametrize("name", NAMES)
def test_analysis_never_executes_the_input(name, tmp_path):
    shell = installed(name)
    marker = tmp_path / "must not exist.txt"
    command = (f"Set-Content -LiteralPath '{marker}' -Value injected" if shell.kind == "powershell"
               else f'echo injected > "{marker.as_posix()}"')
    analysis = analyze_shell(command, shell)
    assert analysis.status == "parsed", analysis
    assert not marker.exists()


@pytest.mark.parametrize("name", ["pwsh", "powershell"])
def test_powershell_ast_preserves_utf8_and_detects_subexpressions(name):
    shell = installed(name)
    analysis = analyze_shell('Write-Output "中文 $(Get-Date)"', shell)
    assert analysis.status == "parsed" and analysis.dynamic and analysis.expansions, analysis
    assert analysis.commands[0].words[1].raw.startswith('"中文 ')
    invalid = analyze_shell('Write-Output (', shell)
    assert invalid.status == "invalid"


@pytest.mark.parametrize("name", ["bash", "sh"])
def test_posix_substitution_and_assignment_block_literal_inference(name):
    shell = installed(name)
    analysis = analyze_shell('NAME=value git status "$(echo bad)"', shell)
    assert analysis.dynamic and analysis.expansions and not analysis.reusable
    assert len(analysis.commands) == 2
    assert not analyze_shell("g* status", shell).reusable
    assert analyze_shell("'git' status", shell).simple_argv == ("git", "status")


def test_unsupported_zsh_and_cmd_control_syntax_are_explicitly_opaque():
    zsh = ShellSpec("zsh", "/bin/zsh", "test", "noninteractive", "0" * 64)
    assert analyze_shell('print -l ${(k)parameters}', zsh).status == "opaque"
    cmd = ShellSpec("cmd", r"C:\Windows\System32\cmd.exe", "test", "noninteractive", "0" * 64)
    assert analyze_shell('for %i in (*) do echo %i', cmd).status == "opaque"
    assert analyze_shell('echo ^& literal', cmd).status == "opaque"
    assert analyze_shell("python - <<EOF\nprint(1)\nEOF", cmd).status == "invalid"


def test_budget_and_missing_parser_never_change_the_selected_dialect(monkeypatch):
    shell = installed("bash")
    assert analyze_shell("x" * 32769, shell).status == "opaque"
    assert analyze_shell("echo \0", shell).status == "invalid"
    assert analyze_shell('echo "', shell).status == "invalid"
    # The same grammar is only a conservative subset of Zsh, not a Zsh syntax oracle.
    assert analyze_shell('echo "', replace(shell, kind="zsh", executable="/bin/zsh")).status == "opaque"
    monkeypatch.setitem(sys.modules, "tree_sitter_bash", None)
    missing = analyze_shell("git status", shell)
    assert missing.status == "opaque" and "unavailable" in missing.reason


def test_analysis_roundtrip_is_bound_to_command_and_full_shell_contract():
    shell = installed("bash")
    analysis = analyze_shell("git status --short", shell)
    restored = ShellAnalysis.from_payload(json.loads(json.dumps(analysis.to_payload())))
    assert restored == analysis and restored.matches("git status --short", shell)
    assert not restored.matches("git status --short; echo extra", shell)
    assert not restored.matches("git status --short", replace(shell, startup="login"))
    payload = restored.to_payload()
    del payload["shell_sha256"]
    with pytest.raises(ValueError):
        ShellAnalysis.from_payload(payload)


def test_native_parser_timeout_is_opaque_without_retry_or_execution(monkeypatch):
    from unittest.mock import Mock
    shell = installed("pwsh")
    run = Mock(side_effect=subprocess.TimeoutExpired("parser", 5))
    monkeypatch.setattr("codey.policies.shell_powershell.cancellation.run_process", run)
    analysis = analyze_shell("git status", shell)
    assert analysis.status == "opaque" and run.call_count == 1
