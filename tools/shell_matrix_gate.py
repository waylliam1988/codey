"""Fail on missing required interpreters and exercise the production shell ticket."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.lifecycle import AppLifecycle
from codey.app.run_registry import RunRegistry
from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket
from codey.app.task_runtime import TaskRuntimeCoordinator
from codey.env_names import SHELL_EXECUTABLE_ENV, SHELL_STARTUP_ENV
from codey.policies.command_line import resolve_shell
from codey.policies.shell_analysis import analyze_shell


class ShellMatrixContext:
    """Exercise the executor with its production lifecycle and spawn owners."""

    def __init__(self) -> None:
        self.task_runtime = TaskRuntimeCoordinator(
            RunRegistry(), ActiveProviderRegistry(), threading.Lock(), threading.Lock(),
            wake_input=lambda: None,
        )

        def stop() -> None:
            self.task_runtime.request_stop()

        self.lifecycle = AppLifecycle(
            stop=stop, close_connections=lambda: None,
            close_conversations=lambda: True, stop_scheduler=lambda: True,
            wait_workers=lambda: True, close_controls=lambda: True, cleanup=(),
        )

    def approval_generation(self) -> int:
        return 0

    def emit(self, event: dict[str, object]) -> None:
        pass


def main() -> int:
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
                    row.update(shell=shell.to_payload(), analysis=analysis.to_payload())
                    context = ShellMatrixContext()
                    try:
                        result = execute_shell_ticket(context, ShellExecutionTicket(
                            command, Path(temporary), 0, 15, 4000, shell, analysis))
                    finally:
                        context.lifecycle.close()
                    expected = 0 if shell.kind == "cmd" else 7
                    row["result"] = result
                    row["ok"] = (analysis.status != "invalid" and result.get("status") == "exit"
                                 and result.get("exit_code") == expected and "SHELL_MATRIX" in result.get("output", ""))
                except Exception as exc:
                    row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                if not row["ok"]:
                    print(json.dumps({"failure": row}), file=sys.stderr)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": sum(bool(row["ok"]) for row in rows), "cases": len(rows)}))
    return 0 if rows and all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
