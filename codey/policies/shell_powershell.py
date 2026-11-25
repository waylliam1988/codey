"""The selected PowerShell engine parses data through a trusted packaged helper."""
from __future__ import annotations

import base64
import subprocess
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from codey.policies.command_line import ShellSpec
from codey.policies.shell_analysis import ShellAnalysis, dynamic_name, parser_identity, shell_identity
from codey.runtime.core import cancellation
from codey.types.json_value import parse_json_object

_HELPER = Path(__file__).with_suffix(".ps1")


def analyze_powershell(command: str, shell: ShellSpec, digest: str) -> ShellAnalysis:
    try:
        cancellation.check()
        return _inspect(command, shell, digest, _helper_source())
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired,
            cancellation.PipeDrainTimeout, cancellation.ProcessOutputReadError) as exc:
        return ShellAnalysis(digest, "opaque", parser_identity(shell),
                             reason=f"PowerShell AST unavailable: {type(exc).__name__}")


@lru_cache(maxsize=1)
def _helper_source() -> str:
    return _HELPER.read_text(encoding="utf-8")


@lru_cache(maxsize=64)
def _inspect(command: str, shell: ShellSpec, digest: str, helper_source: str) -> ShellAnalysis:
    # Only the packaged helper is executable. The command remains a data argument.
    source = base64.b64encode(command.encode("utf-8")).decode("ascii")
    program = "& {\n" + helper_source + "\n} -SourceBase64 '" + source + "'"
    encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
    result = cancellation.run_process([shell.executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                                       "-EncodedCommand", encoded], cwd=_HELPER.parent, timeout=5,
                                      capture_limit_bytes=131072)
    if result.returncode != 0 or result.stdout_truncated:
        raise ValueError("PowerShell AST inspection failed or was truncated")
    payload: dict[str, object] = dict(parse_json_object(result.stdout))
    payload.update(command_sha256=digest, shell_sha256=shell_identity(shell), parser=parser_identity(shell))
    analysis = ShellAnalysis.from_payload(payload)
    return replace(analysis, dynamic=analysis.dynamic or any(dynamic_name(row.words) for row in analysis.commands),
                   reason=analysis.reason[:240])
