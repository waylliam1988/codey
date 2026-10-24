"""Fail on missing required interpreters and exercise the production shell ticket."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket
from codey.env_names import SHELL_EXECUTABLE_ENV, SHELL_STARTUP_ENV
from codey.policies.command_line import resolve_shell
from codey.policies.shell_analysis import analyze_shell


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--required", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix="shell-matrix-") as temporary:
        for name in args.required:
            for mode in ("noninteractive", "login") if name != "cmd" else ("noninteractive",):
                row = {"name": name, "startup": mode, "ok": False}
                try:
                    shell = resolve_shell(environ={**os.environ, SHELL_EXECUTABLE_ENV: name, SHELL_STARTUP_ENV: mode})
                    command = {"cmd": "echo SHELL_MATRIX", "powershell": "Write-Output 'SHELL_MATRIX'; exit 7"}.get(
                        shell.kind, "printf '%s\\n' SHELL_MATRIX; exit 7")
                    analysis = analyze_shell(command, shell)
                    context = SimpleNamespace(_shell_spawn_gate=threading.Lock(), approval_generation=lambda: 0,
                                              run_registry=SimpleNamespace(stop_flag=threading.Event()))
                    result = execute_shell_ticket(context, ShellExecutionTicket(command, Path(temporary), 0, 15, 4000,
                                                                               shell, analysis))
                    expected = 0 if shell.kind == "cmd" else 7
                    row.update(shell=shell.to_payload(), analysis=analysis.to_payload(), result=result)
                    row["ok"] = (analysis.status != "invalid" and result.get("status") == "exit"
                                 and result.get("exit_code") == expected and "SHELL_MATRIX" in result.get("output", ""))
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": sum(bool(row["ok"]) for row in rows), "cases": len(rows)}))
    return 0 if rows and all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
