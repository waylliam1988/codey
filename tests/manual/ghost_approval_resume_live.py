"""Real local-model Shell approval/resume with a changing Ghost projection."""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from codey.app import api
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.ghost_context import ghost_continuity
from codey.operations.task_entry import run_task_submission
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from codey.task.model import ResolveShellApproval, StartTask, TaskSubmission


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    config = LocalProviderConfig(base_url="http://127.0.0.1:5001/v1", native_tools_mode="on",
        thinking_enabled=False, context=LocalContextBudget(262144, 32768, 12000))
    state = AppContext(directory / "state", start_scheduler=False)
    provider = connect_local(config=config)
    provider.timeout = 300
    provider.temperature = 0.3
    events, payloads = [], []
    configure = provider.configure_request

    def record(name, value):
        with (directory / name).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")

    def observed(payload):
        configure(payload)
        payload["seed"] = 74
        payloads.append(payload)
        record("wire.jsonl", {"time": time.time(), "payload": payload})

    def emit(event):
        events.append(event)
        record("events.jsonl", event)

    provider.configure_request = observed
    state.emit = emit
    task = "Check the Python version. Request exactly python --version through shell, wait for approval, then report only the observed version."
    request = TaskSubmission.from_input(session_id="approval-live", project=None, max_turns=6, provider_id="local",
        input=StartTask(task), intent="chat", run_id="approval-live-run", denied_capabilities=("project.write",))
    deps = build_task_deps(state, connect_provider=lambda _: provider)
    try:
        state.ghost_continuity.sync_from_sources(user_focus_excerpt="Previous focus: SQLite documentation.",
            session_id=request.session_id, run_id="previous", project="", mode="chat")
        before = ghost_continuity(state, session_id=request.session_id).text
        run_task_submission(deps, request)
        first = [event for event in events if event.get("type") == "task_done"][-1]
        if first.get("stop_reason") != "approval":
            raise RuntimeError(first)
        # Use the real projection writer while the original tool call is paused.
        state.ghost_continuity.sync_from_sources(user_focus_excerpt="Current focus: Python interpreter version.",
            session_id=request.session_id, run_id=request.run_id, project="", mode="chat")
        after = ghost_continuity(state, session_id=request.session_id).text
        if not before or before == after:
            raise RuntimeError("probe did not change continuity")
        approval_id = next(iter(state.durable.task(request.run_id).state["approvals"]))
        status, _ = api.shell_approval_response(state, {"id": approval_id, "approved": True},
                                               submit_task_after_slot_release=lambda *_: None)
        if status != 202 or not state.shell_approval_workflow.wait(30):
            raise RuntimeError("approval did not settle")
        run_task_submission(deps, replace(request, input=ResolveShellApproval(approval_id)))
        final = [event for event in events if event.get("type") == "task_done"][-1]
        effects = state.durable.effects(request.run_id)
        result = {"model": provider.model, "first": first, "final": final,
            "continuity_changed": before != after, "requests": len(payloads),
            "all_effects_settled": all(effect.settlement is not None for effect in effects),
            "approval": state.resolve_shell_approval(approval_id)}
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True, default=str), flush=True)
        return 0 if final.get("stop_reason") == "done" and result["all_effects_settled"] else 1
    finally:
        provider.close()
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
