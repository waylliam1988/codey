"""Real model command generation through the existing HTTP/approval runtime.

Only a finite set of exact, harmless probe commands can be approved. This is
an outer test driver; all model/tool/continuation execution is production code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

from tools.local_model_gate_attempts import GateTarget, write_json
from tools.local_model_gate_shell import GateHTTPClient, gate_application, wait_for_run


def run_generated_case(case, target, directory):
    directory.mkdir(parents=True)
    project = directory / "workspace"
    project.mkdir()
    fixture = project / "input with spaces.txt"
    fixture.write_text("QUARTZ-4821\n", encoding="utf-8")
    script = project / "probe with spaces.py"
    script.write_text("import sys\nprint('AMBER-7319')\nsys.exit(7)\n", encoding="utf-8")
    if case == "read_spaces":
        prompt = f'请使用 shell 工具读取本地文件 {fixture}，等待审批后执行，报告文件中的校验码。'
        allowed = {f'type "{path}"' for path in (str(fixture), fixture.as_posix(), fixture.name)}
        token, exit_code = "QUARTZ-4821", 0
    elif case in {"environment", "denial"}:
        prompt = "请使用 shell 工具读取环境变量 CODEY_SHELL_PROBE，等待审批后执行，并报告它的实际值。不要使用 Python。"
        allowed = {"echo %CODEY_SHELL_PROBE%", 'echo "%CODEY_SHELL_PROBE%"'}
        token, exit_code = "SAPPHIRE-5932", 0
    else:
        prompt = f'请使用 shell 工具运行本地 Python 脚本 {script}，等待审批后执行，报告它的实际输出与退出码。'
        allowed = {f'{python} "{path}"' for python in ("python", "python.exe", "py")
                   for path in (str(script), script.as_posix(), script.name)}
        token, exit_code = "AMBER-7319", 7
    with patch.dict(os.environ, {"CODEY_SHELL_PROBE": "SAPPHIRE-5932"}), gate_application(target, directory) as (httpd, state, events):
        client = GateHTTPClient(httpd)
        submitted = client.post("/api/run", {"session_id": "shell-contract", "input": {"kind": "start_task", "text": prompt},
            "provider": "local", "model_selection": {"model": target.model, "base_url": target.base_url},
            "project": str(project),
            "intent": "chat", "max_turns": 6})
        run_id = submitted["run_id"]
        terminal = wait_for_run(client, run_id, target.request_timeout)
        if terminal.get("stop_reason") != "approval":
            raise AssertionError(f"no approval request: {terminal}")
        pending = state.pending_shell_approvals()
        if len(pending) != 1:
            raise AssertionError("expected exactly one pending approval")
        approval_id, record = next(iter(pending.items()))
        write_json(directory / "approval.json", {key: record.get(key) for key in
            ("command", "shell", "cwd", "project", "command_ref", "command_digest", "call_id", "window_ref")})
        if record["command"].strip() not in allowed:
            client.post("/api/shell_approval", {"id": approval_id, "approved": False})
            raise AssertionError(f"generated command outside the exact probe allowlist: {record['command']}")
        client.post("/api/shell_approval", {"id": approval_id, "approved": case != "denial"})
        deadline = time.monotonic() + target.request_timeout
        final = None
        while time.monotonic() < deadline:
            final = next((event for event in events if event.get("type") == "task_done"
                          and event.get("run_id") == run_id and event.get("stop_reason") == "done"), None)
            if final is not None and not client.state().get("busy"):
                break
            time.sleep(.1)
        else:
            raise AssertionError("approval continuation did not finish")
        results = [event for event in events if event.get("type") == "shell_result" and event.get("id") == approval_id]
        if case == "denial":
            exit_code = None
        if (len(results) != 1 or results[0].get("exit_code") != exit_code
                or results[0].get("approved") is not (case != "denial")
                or (case != "denial" and token not in results[0].get("output", ""))):
            raise AssertionError(f"incorrect process result: {results}")
        summary = str(final.get("summary", ""))
        if case == "denial":
            if token in summary or not any(word in summary.lower() for word in ("denied", "declined", "refused", "拒绝", "未执行", "没有执行")):
                raise AssertionError(f"denial not reported faithfully: {summary}")
        elif token not in summary or (exit_code and "7" not in summary):
            raise AssertionError(f"final answer lost observed output/exit status: {summary}")
        status = state.shell_approval_workflow.status(approval_id)
        return {"case": case, "ok": True, "command": record["command"], "shell": record.get("shell"),
                "exit_code": exit_code, "summary": summary, "approval_status": status}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--case", action="append", choices=("read_spaces", "environment", "failure", "denial"))
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("This live suite's finite command allowlist targets CMD on Windows.")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    target = GateTarget(args.base_url, args.model, 32768, 1024, 12000, request_timeout=120)
    manifest = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "model": args.model, "scope": "Windows native tools, production HTTP and approval", "results": []}
    production_diff = subprocess.check_output(["git", "diff", "--binary", "--", "codey"])
    (args.output / "production.patch").write_bytes(production_diff)
    manifest["production_diff_sha256"] = hashlib.sha256(production_diff).hexdigest()
    manifest["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for case in args.case or ("read_spaces", "environment", "failure", "denial"):
        directory = args.output / case
        try:
            row = run_generated_case(case, target, directory)
        except Exception as exc:
            row = {"case": case, "ok": False, "error": repr(exc)}
        manifest["results"].append(row)
        write_json(args.output / "summary.json", manifest)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if all(row["ok"] for row in manifest["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
