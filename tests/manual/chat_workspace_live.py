"""Live KoboldCpp smoke: ordinary Chat, workspace edit, and paused Shell in both protocols.

Run: python -m tests.manual.chat_workspace_live --base-url http://127.0.0.1:5001/v1
Only isolated temporary workspace files are written. Shell commands are never approved.
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from codey.agents.handoff import ConversationContext
from codey.app.context import AppContext
from codey.operations.context import RunFrame, RunWork
from codey.operations.task_entry import run_entry_kernel
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes


def probe(base_url: str, model: str, protocol: str, case: str) -> dict[str, object]:
    task = {
        "greeting": "Say hello in one short sentence. Do not inspect files or run commands.",
        "edit": "Create hello.txt containing exactly workspace-live. Do not execute commands. Then report the file you created.",
        "shell": "Check the Python version on this computer. Request the command python --version through shell and wait for approval.",
    }[case]
    denied = ("project.verify", "shell.approval") if case != "shell" else ("project.write",)
    config = LocalProviderConfig(base_url=base_url, model=model, native_tools_mode=protocol,
                                 context=LocalContextBudget(32768, 2048, 12000), thinking_enabled=False)
    started = time.monotonic()
    observations = []
    with (
        tempfile.TemporaryDirectory(prefix="codey-chat-live-") as temp,
        closing(AppContext(Path(temp) / "state")) as state,
        closing(connect_local(config=config)) as provider,
    ):
        provider.bind_usage("local", observations.append)
        request = TaskSubmission("live-chat", None, task, 4, False, "local", intent="chat",
                                 run_id="live-run", denied_capabilities=denied)
        frame = RunFrame(request, request.run_id, "chat", provider, "local", "", ConversationContext(),
                         True, "", "", None, "", False, set(), 0)
        events, approvals = [], []
        result = run_entry_kernel(frame, RunWork([], ExecutionEvidence()),
                                  SimpleNamespace(on_event=events.append, on_shell_request=approvals.append),
                                  SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions,
                                                  collect_changes=collect_changes))
        target = Path(frame.project_text) / "hello.txt"
        content = target.read_text(encoding="utf-8") if target.exists() else ""
        ok = ((result.event["stop_reason"] == "done" and len(observations) == 1 and not approvals)
              if case == "greeting" else
              (result.event["stop_reason"] == "done" and content == "workspace-live")
              if case == "edit" else
              (result.event["stop_reason"] == "approval" and len(approvals) == 1))
        return {"protocol": protocol, "case": case, "ok": ok,
                "stop_reason": result.event["stop_reason"], "turns": result.event["turns"],
                "physical_requests": len(observations), "approval_requests": len(approvals),
                "summary": str(result.event["summary"])[:600], "seconds": round(time.monotonic() - started, 2)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    results = []
    for protocol in ("on", "off"):
        for case in ("greeting", "edit", "shell"):
            try:
                result = probe(args.base_url, args.model, protocol, case)
            except Exception as exc:
                result = {"protocol": protocol, "case": case, "ok": False, "error": str(exc)[:600]}
            results.append(result)
            print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
