"""The selected PowerShell engine parses data through a trusted packaged helper."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis, ShellCommand, ShellRedirect, dynamic_name
from codey.runtime.core import cancellation


def analyze_powershell(command: str, shell: ShellSpec, digest: str) -> ShellAnalysis:
    parser = "PowerShell AST/" + (shell.version or "unknown")
    helper = Path(__file__).with_suffix(".ps1")
    source = base64.b64encode(command.encode("utf-8")).decode("ascii")
    try:
        # Only trusted packaged code is executable. The input is a Base64 data
        # argument with no PowerShell metacharacters, never a script fragment.
        program = "& {\n" + helper.read_text(encoding="utf-8") + "\n} -SourceBase64 '" + source + "'"
        encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
        result = cancellation.run_process([shell.executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                                           "-EncodedCommand", encoded], cwd=helper.parent, timeout=5,
                                          capture_limit_bytes=131072)
        if result.returncode != 0 or result.stdout_truncated:
            return ShellAnalysis(digest, "opaque", parser, reason="PowerShell AST inspection failed or was truncated")
        return _decode(json.loads(result.stdout), digest, parser)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired,
            cancellation.PipeDrainTimeout, cancellation.ProcessOutputReadError) as exc:
        return ShellAnalysis(digest, "opaque", parser, reason=f"PowerShell AST unavailable: {type(exc).__name__}")


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("invalid AST strings")
    return tuple(value)


def _decode(value: Any, digest: str, parser: str) -> ShellAnalysis:
    if not isinstance(value, dict) or value.get("status") not in {"parsed", "opaque", "invalid"}:
        raise ValueError("invalid AST result")
    if not isinstance(value.get("commands"), list) or not isinstance(value.get("redirections"), list):
        raise ValueError("invalid AST commands")
    if type(value.get("dynamic")) is not bool or not isinstance(value.get("reason"), str):
        raise ValueError("invalid AST flags")
    commands = tuple(ShellCommand.from_payload(row) for row in value["commands"])
    redirects = tuple(ShellRedirect.from_payload(row) for row in value["redirections"])
    dynamic = value["dynamic"] or any(dynamic_name(row.words) for row in commands)
    return ShellAnalysis(digest, value["status"], parser, commands, _strings(value["operators"]), redirects,
                         _strings(value["expansions"]), dynamic, value["reason"][:240])
