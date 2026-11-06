"""Drive an isolated native Codey window through reproducible tool events.

Run with ``python -m tests.manual.tool_stream_review --artifacts <new-folder>``.
Write stage numbers 0..7 to <new-folder>/advance.txt; write ``quit`` to close.
The production frontend receives real HTTP/SSE and durable history. Tool results
are controlled fixtures, not model execution or real file/command side effects.
"""
from __future__ import annotations

import argparse
import json
import threading
from pathlib import Path

from codey.app.service import LocalService

CHAT = "tool-stream-review"
RUN = "tool-stream-review-run"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    service = LocalService(artifacts / "state", verify_only=True, release_id="source:tool-stream-review").start()
    ctx = service.ctx
    httpd = service.httpd
    ctx.durable.admit_input(conversation_id=CHAT, request_id="tool-stream-review-input", task_id=RUN,
                            text="Fix the login form and verify the changes.",
                            provider_snapshot={"provider_id": "local", "model_id": "review"})
    ctx.durable.start_task(RUN, state={})
    ctx.save_ui_state({"active_id": CHAT, "sessions": [
        {"id": CHAT, "title": "Tool stream review", "provider": "local", "messages": []}
    ], "projects": []}, base_revision=0)
    ctx.reserve_run(session_id=CHAT, project=None, task="Tool stream review", provider_id="local", run_id=RUN)

    import webview
    window = webview.create_window("Codey | Tool stream review",
                                  url=httpd.launch_url(f"http://127.0.0.1:{httpd.server_port}/"),
                                  width=1380, height=950, text_select=True)
    assert window is not None
    stopped = threading.Event()
    window.events.closed += stopped.set
    trace: list[dict] = []

    def emit(event_type: str, **fields: object) -> None:
        event = {"type": event_type, "session_id": CHAT, "run_id": RUN, **fields}
        ctx.emit(event)
        trace.append(event)

    def tool(call: str, kind: str, path: str = "", *, parent: str = "", pending: bool = True) -> None:
        emit("tool_started" if pending else "tool", call_id=call, parent_call_id=parent,
             tool_name=kind, kind=kind, path=path, turn=1, activity="Inspecting login form",
             status="ok", ok=True, changed=kind == "edit" and not pending,
             result="Retained result", detail="Verified fixture content for " + (path or "CodeMode"))

    def command(call: str, *, pending: bool, exit_code: int = 0) -> None:
        status = "running" if pending else "exit"
        output = "Running unit tests" if pending else "One test failed" if exit_code else "All tests passed"
        # Presentation fixtures retain command observations without granting an
        # approval or executing a shell command.
        ctx.durable.record_display_message(CHAT, run_id=RUN, identity=f"command:{call}:{status}", payload={
            "type": "shell_result", "runId": RUN, "callId": call, "id": call,
            "command": "npm run test:unit", "approved": True, "shellStatus": status,
            "exitCode": None if pending else exit_code, "output": output,
        })
        emit("shell_result", id=call, call_id=call, command="npm run test:unit", approved=True,
             status=status, exit_code=None if pending else exit_code, output=output)

    def stage(index: int) -> None:
        if index == 0:
            emit("task_start", provider="local", mode="code", max_turns=8)
            tool("codemode", "codemode")
            tool("read-a", "read", "src/LoginForm.tsx", parent="codemode")
            tool("read-b", "read", "src/login.test.ts", parent="codemode")
        elif index == 1:
            tool("read-b", "read", "src/login.test.ts", parent="codemode", pending=False)
        elif index == 2:
            tool("read-a", "read", "src/LoginForm.tsx", parent="codemode", pending=False)
            tool("edit", "edit", "src/LoginForm.tsx", parent="codemode")
        elif index == 3:
            tool("edit", "edit", "src/LoginForm.tsx", parent="codemode", pending=False)
            tool("read-c", "read", "src/LoginForm.tsx", parent="codemode")
        elif index == 4:
            tool("command", "shell")
            command("command", pending=True)
        elif index == 5:
            tool("read-c", "read", "src/LoginForm.tsx", parent="codemode", pending=False)
            tool("codemode", "codemode", pending=False)
        elif index == 6:
            command("command", pending=False, exit_code=1)
        elif index == 7:
            tool("retry-command", "shell")
            command("retry-command", pending=False)
            ctx.durable.complete_task(RUN, "Updated the login form. The follow-up checks passed.", state={})
            emit("task_done", stop_reason="done", summary="Updated the login form. The follow-up checks passed.", turns=1)
            ctx.release_run(RUN)
        (artifacts / "events.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
        (artifacts / "history.json").write_text(json.dumps(ctx.durable.history_page(CHAT), indent=2), encoding="utf-8")
        (artifacts / "stage.txt").write_text(str(index), encoding="utf-8")
        print(f"Stage {index} emitted", flush=True)

    def control() -> None:
        current = -1
        control_file = artifacts / "advance.txt"
        while not stopped.wait(0.2):
            if not control_file.exists():
                continue
            value = control_file.read_text(encoding="utf-8").strip()
            if value == "quit":
                window.destroy()
                return
            if value.isdigit() and current < int(value) <= 7:
                for index in range(current + 1, int(value) + 1):
                    stage(index)
                current = int(value)

    try:
        print(f"Native review ready: {artifacts}", flush=True)
        webview.start(func=control, private_mode=True, storage_path=str(artifacts / "webview"))
    finally:
        stopped.set()
        ctx.release_run(RUN)
        service.close()


if __name__ == "__main__":
    main()
