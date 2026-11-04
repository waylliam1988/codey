"""Live local-model approval/resume checks, with warm and cold provider history.

Runs only an approved echo command in an isolated Codey state directory.
Keeps final HTTP payloads and events for inspecting task/call identity.
"""
from __future__ import annotations

import argparse
import copy
import json
import threading
from pathlib import Path
from unittest.mock import patch

from codey.app import api
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.task_entry import run_task_submission
from codey.providers import api_generation
from codey.providers.local_connection import capture_selection, config_for_selection, open_selection
from codey.task.model import StartTask, TaskSubmission


def probe(directory: Path, *, cold: bool) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=False)
    default = capture_selection()
    selection = capture_selection({"base_url": config_for_selection(default).base_url, "model": default.model_id,
                                   "effort": "medium", "thinking": True})
    provider = open_selection(selection)
    provider.timeout = 180
    providers = [provider]
    payloads, events, workers = [], [], []
    queued = threading.Event()
    terminal = threading.Event()
    marker = "APPROVAL_MARKER_739142"
    command = f"echo {marker}"
    task = (f"Local approval diagnostic. Use shell to execute exactly `{command}` once, wait for approval, "
            "then report the observed marker using done. Do not execute the command again. "
            "No other commands, file changes, or network access are needed.")
    original_generate = api_generation.generate

    def generate(url, payload, *args, **kwargs):
        payloads.append(copy.deepcopy(payload))
        print(json.dumps({"cold": cold, "request": len(payloads), "messages": len(payload.get("messages", []))}), flush=True)
        return original_generate(url, payload, *args, **kwargs)

    def enqueue(fn, *args, **kwargs):
        workers.append(lambda: fn(*args, **kwargs))
        queued.set()
        return True

    state = AppContext(directory / "state")
    state.get_provider = lambda *_args: providers[-1]

    def observe(_sequence, event):
        events.append(copy.deepcopy(event))
        if event.get("type") == "task_done":
            terminal.set()

    state.event_bus.observe(observe)
    request = TaskSubmission.from_input(session_id="approval-live", project=None, max_turns=4, provider_id="local",
        input=StartTask(task), intent="chat", model_selection=selection.to_payload())
    try:
        with patch.object(api_generation, "generate", generate), patch("codey.app.task_submit.submit_browser_task", enqueue):
            run_task_submission(build_task_deps(state), request)
            pending = state.pending_shell_approvals()
            assert len(pending) == 1, [event for event in events if event.get("type") == "task_done"]
            approval_id, record = next(iter(pending.items()))
            assert record["command"].strip() == command, record["command"]
            if cold:
                provider.close()
                restored = open_selection(selection)
                restored.timeout = 180
                providers.append(restored)
            terminal.clear()
            status, _ = api.shell_approval_response(state, {"id": approval_id, "approved": True},
                                                    submit_task_after_slot_release=lambda *_args: None)
            assert status == 202 and state.shell_approval_workflow.wait(30)
            assert queued.wait(15) and len(workers) == 1
            workers.pop()()
            assert terminal.wait(5)
        final = [event for event in events if event.get("type") == "task_done"][-1]
        assert final["stop_reason"] == "done", final
        assert marker in final.get("summary", ""), final
        assert len(payloads) == 2, len(payloads)
        resumed = payloads[1]["messages"]
        results = [message for message in resumed if message.get("role") == "tool"]
        assert len(results) == 1 and results[0]["tool_call_id"] == record["call_id"]
        assert marker in results[0]["content"]
        observations = [str(message.get("content", "")) for message in resumed
                        if "Current work observations (runtime records):" in str(message.get("content", ""))]
        assert len(observations) == 1 and json.dumps(task) in observations[0]
        assert "Resolve the shell approval" not in observations[0]
        cards = [event for event in events if event.get("type") == "shell_result"]
        assert len(cards) == 2 and all(event["call_id"] == record["call_id"] for event in cards)
        starts = [event for event in events if event.get("type") == "task_start"]
        assert len(starts) == 2 and all(event["task"] == task for event in starts)
        result = {"cold_provider": cold, "model": provider.model, "effort": "medium", "requests": len(payloads),
                  "shell_executions": len({event["execution_id"] for event in cards}),
                  "original_task_preserved": True, "tool_result_paired": True,
                  "stop_reason": final["stop_reason"], "summary": final.get("summary"), "passed": True}
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        (directory / "wire.json").write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
        for item in providers:
            item.close()
        state.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    for cold in (False, True):
        result = probe(args.directory.resolve() / ("cold" if cold else "warm"), cold=cold)
        print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
