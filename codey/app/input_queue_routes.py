"""HTTP boundary and application lifecycle binding for conversation inputs."""
from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

from codey.app import api, task_submit
from codey.runtime.core.api_selection import ApiRunSelection
from codey.task.entry_auth import EntryAuth


def bind_input_queue(ctx: Any) -> None:
    def ready() -> bool:
        first = next(
            (
                item
                for item in ctx.input_queue.list()
                if item["state"] == "queued"
                and ctx.active_run_for(session_id=str(item["request"].get("session_id") or "")) is None
                and not ctx.stop_flag_for(str(item["request"].get("run_id") or "")).is_set()
            ),
            None,
        )
        return not ctx.closed and first is not None and not (
            first and ctx.user_questions.pending(first["request"]["session_id"])
        ) and not any(
            item["decision"] == "pending" or item["continuation"] in {"queued", "resuming"}
            for item in ctx.shell_approval_workflow.list_statuses()
        )

    def submit(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        selection = ApiRunSelection.from_payload(body["captured_selection"]) if "captured_selection" in body else None
        auth = EntryAuth(**body["captured_auth"]) if "captured_auth" in body else None
        result = api.run_submit_response(body, partial(task_submit.submit_task, get_state=lambda: ctx, abort_if_stopped=True), ctx=ctx, captured_selection=selection, captured_auth=auth)
        request_run_id = str(body.get("run_id") or "")
        if result[0] == 409 and ctx.stop_flag_for(request_run_id).is_set():
            return 409, {"error": "stopped"}
        return result

    ctx.input_queue.bind(ready, submit)


def inputs_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    bind_input_queue(ctx)
    identity = str((query.get("id") or [""])[0])
    if identity:
        item = ctx.input_queue.get(identity)
        if item is None:
            return 404, {"ok": False, "error": "input_not_found"}
        run = ctx.run_state_payload()
        terminal = run.get("last_terminal_event")
        if isinstance(terminal, dict) and terminal.get("run_id") == item["request"]["run_id"]:
            ctx.input_queue.completed(terminal["run_id"], terminal)
            item = ctx.input_queue.get(identity)
        return 200, {"ok": True, "item": item}
    return 200, {"ok": True, "items": ctx.input_queue.list(str((query.get("session_id") or [""])[0]))}


def input_action_response(ctx: Any, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    bind_input_queue(ctx)
    identity = str(body.get("id") or "")
    action = body.get("action")
    try:
        if action == "create":
            source = body.get("request")
            if not isinstance(source, dict):
                raise ValueError("request required")
            request = {key: source[key] for key in ("session_id", "run_id", "project", "task", "provider", "intent", "model_selection", "max_turns", "input_mode") if key in source}
            if request.get("input_mode", "follow_up") not in {"steer", "follow_up"}:
                raise ValueError("invalid input mode")
            state = ctx.load_ui_state()
            session = next((session for session in state["sessions"] if session["id"] == request.get("session_id")), None)
            if session is None:
                raise ValueError("session not found")
            project = next((project for project in state["projects"] if project["id"] == session.get("projectId")), None)
            expected = str(Path(project["path"]).resolve()) if project else ""
            actual = str(Path(request["project"]).resolve()) if request.get("project") else ""
            if expected != actual:
                raise ValueError("input workspace changed")
            item = ctx.input_queue.create(identity, request)
        else:
            revision = body.get("revision")
            if type(revision) is not int:
                raise ValueError("input revision required")
            item = ctx.input_queue.get(identity)
            if item is None:
                return 404, {"ok": False, "error": "input_not_found"}
            if action == "edit":
                if not isinstance(body.get("text"), str):
                    raise ValueError("input text required")
                item = ctx.input_queue.edit(identity, revision, body["text"])
            elif action == "remove":
                item = ctx.input_queue.remove(identity, revision)
            elif action == "resume":
                if ctx.active_run_for(session_id=str(item["request"].get("session_id") or "")) is not None:
                    return 409, {"ok": False, "error": "task still stopping"}
                with ctx._shell_spawn_gate:
                    item = ctx.input_queue.resume(identity, revision)
                    ctx.run_registry.clear_stop(str(item["request"].get("run_id") or ""))
            else:
                raise ValueError("invalid input action")
        return 202, {"ok": True, "item": item}
    except ValueError as exc:
        return 409, {"ok": False, "error": str(exc)}
