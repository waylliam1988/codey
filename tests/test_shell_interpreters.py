"""Real installed interpreters share the production ticket/process lifecycle."""
from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import replace

import pytest

from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket
from codey.env_names import SHELL_EXECUTABLE_ENV, SHELL_STARTUP_ENV
from codey.policies.command_line import ShellSpec, resolve_shell
from tests.test_shell_contract import _context

NAMES = ("cmd", "pwsh", "powershell", "bash", "zsh", "sh")


def installed(name, startup="noninteractive"):
    try:
        return resolve_shell(environ={**os.environ, SHELL_EXECUTABLE_ENV: name, SHELL_STARTUP_ENV: startup})
    except ValueError as exc:
        pytest.skip(f"interpreter {name} unavailable on this host: {exc}")


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("startup", ["noninteractive", "login"])
def test_real_interpreter_quotes_unicode_directory_and_native_exit(tmp_path, name, startup):
    if name == "cmd" and startup == "login":
        with pytest.raises(ValueError, match="startup"):
            resolve_shell(environ={**os.environ, SHELL_EXECUTABLE_ENV: name, SHELL_STARTUP_ENV: startup})
        return
    shell = installed(name, startup)
    assert shell.binary_sha256 and (shell.version or shell.kind == "sh")
    assert ShellSpec.from_payload(shell.to_payload()) == shell
    work = tmp_path / "folder with spaces"
    work.mkdir()
    (work / "probe file.py").write_text(
        "import json,os,sys\nprint(json.dumps([sys.argv[1],os.path.basename(os.getcwd()),'中文'],ensure_ascii=False))\nsys.exit(7)",
        encoding="utf-8")
    python = sys.executable.replace("\\", "/")
    if shell.kind == "cmd":
        command = f'"{python}" "probe file.py" "two words"'
    elif shell.kind == "powershell":
        command = f"& '{python}' 'probe file.py' 'two words'; exit $LASTEXITCODE"
    else:
        command = f"'{python}' 'probe file.py' 'two words'"
    result = execute_shell_ticket(_context(), ShellExecutionTicket(command, work, 0, 15, 4000, shell=shell))
    assert result["status"] == "exit" and result["exit_code"] == 7, result
    assert json.loads(result["output"]) == ["two words", work.name, "中文"]


@pytest.mark.parametrize("name", NAMES)
def test_startup_parameters_are_owned_by_the_contract(name):
    shell = installed(name)
    invocation = shell.invocation("echo literal")
    if shell.kind == "powershell":
        assert isinstance(invocation, list)
        assert "-NoProfile" in invocation and "-NonInteractive" in invocation
        script = base64.b64decode(invocation[-1]).decode("utf-16-le")
        assert script.endswith("\necho literal")
        assert "-NoProfile" not in replace(shell, startup="login").invocation("echo literal")
    elif shell.kind != "cmd":
        assert invocation[-2:] == ["-c", "echo literal"]
        assert replace(shell, startup="login").invocation("echo literal")[-2:] == ["-lc", "echo literal"]


def test_changed_binary_is_rejected_before_any_spawn(tmp_path, monkeypatch):
    from codey.app import shell_service
    shell = resolve_shell()
    start = []
    monkeypatch.setattr("codey.runtime.core.shell_discovery.binary_fingerprint", lambda path: "f" * 64)
    monkeypatch.setattr(shell_service.cancellation, "start_process", lambda *a, **kw: start.append(a))
    result = execute_shell_ticket(_context(), ShellExecutionTicket("echo harmless", tmp_path, 0, 10, 4000, shell=shell))
    assert not start and not result["ok"] and result["status"] == "spawn_error"
    assert "changed after approval" in result["error"]


def test_missing_metadata_is_not_upgraded_from_the_current_host():
    shell = resolve_shell()
    for key in ("version", "startup", "binary_sha256"):
        payload = shell.to_payload()
        del payload[key]
        with pytest.raises(ValueError):
            ShellSpec.from_payload(payload)
