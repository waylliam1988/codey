"""Compare the same real shell ticket before/after COMSPEC changes on Windows."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codey.app.shell_service import ShellExecutionTicket, execute_shell_ticket


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("COMSPEC drift is a Windows boundary")
    context = SimpleNamespace(_shell_spawn_gate=threading.Lock(), approval_generation=lambda: 0,
                              run_registry=SimpleNamespace(stop_flag=threading.Event()))
    with tempfile.TemporaryDirectory(prefix="codey-shell-drift-") as temporary:
        ticket = ShellExecutionTicket(command='python -c "print(\'SHELL_PINNED\')"', cwd=Path(temporary),
                                      generation=0, timeout=10, output_limit=4000)
        with patch.dict(os.environ, {"COMSPEC": str(Path(temporary) / "missing cmd.exe")}):
            result = execute_shell_ticket(context, ticket)
    row = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
           "result": result, "passed": result.get("exit_code") == 0 and "SHELL_PINNED" in result.get("output", "")}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
