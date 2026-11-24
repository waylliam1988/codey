"""HTTP boundary and application lifecycle binding for conversation inputs."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from codey.app.input_queue import InputItem, capture_input_selection
from codey.utils.object_fields import object_fields, require_object_rows

if TYPE_CHECKING:
    from codey.app.context import AppContext


def bind_input_queue(ctx: AppContext) -> None:
    # Compatibility entry point; DurableTaskScheduler owns dispatch. The queue's
    # bind method never installs callbacks or starts a second worker.
    return None


def _resolved_workspace(value: object) -> str:
    if not isinstance(value, (str, Path)):
        raise TypeError("workspace path must be text or a Path")
    return str(Path(value).resolve())


def _draining(ctx: AppContext) -> bool:
    return ctx.control_service is not None and ctx.control_service.draining


def inputs_response(ctx: AppContext, query: Mapping[str, list[str]]) -> tuple[int, dict[str, object]]:
    bind_input_queue(ctx)
    identity = (query.get("id") or [""])[0]
    if identity:
        item = ctx.input_queue.get(identity)
        if item is None:
            return 404, {"ok": False, "error": "input_not_found"}
        run = ctx.run_state_payload()
        terminal = run.get("last_terminal_event")
        if object_fields(terminal) and terminal.get("run_id") == item["request"]["run_id"]:
            ctx.input_queue.completed(str(terminal["run_id"]), terminal)
            item = ctx.input_queue.get(identity)
        return 200, {"ok": True, "item": item}
    return 200, {"ok": True, "items": ctx.input_queue.list((query.get("session_id") or [""])[0])}


def input_action_response(ctx: AppContext, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
    bind_input_queue(ctx)
    identity = str(body.get("id") or "")
    action = body.get("action")
    item: InputItem | None
    try:
        if action == "create":
            if _draining(ctx):
                return 503, {"ok": False, "error": "server_draining"}
            source = body.get("request")
            if not object_fields(source):
                raise ValueError("request required")
            request = {key: source[key] for key in ("session_id", "run_id", "project", "task", "provider", "intent", "requested_capabilities", "model_selection", "max_turns", "input_mode", "attachments") if key in source}
            mode = request.get("input_mode", "follow_up")
            if not isinstance(mode, str) or mode not in {"steer", "follow_up"}:
                raise ValueError("invalid input mode")
            state = ctx.load_ui_state()
            session = next((session for session in require_object_rows(state["sessions"]) if session["id"] == request.get("session_id")), None)
            if session is None:
                raise ValueError("session not found")
            from codey.app.fork_workspaces import ensure_workspace_ready

            ensure_workspace_ready(ctx, str(session['id']))
            project = next((project for project in require_object_rows(state["projects"]) if project["id"] == session.get("projectId")), None)
            expected = _resolved_workspace(project["path"]) if project else ""
            actual = _resolved_workspace(request["project"]) if request.get("project") else ""
            if expected != actual:
                raise ValueError("input workspace changed")
            selection = capture_input_selection(str(request.get("provider") or ""), request.get("model_selection"))
            from codey.app.attachment_routes import prepare_queued_attachments

            selection, message_metadata, attachment_error = prepare_queued_attachments(ctx, request, selection)
            if attachment_error:
                return attachment_error
            with ctx.task_runtime.spawn_line():
                if _draining(ctx):
                    return 503, {"ok": False, "error": "server_draining"}
                item = ctx.input_queue.create(identity, request, model_selection=selection, **({"message_metadata": message_metadata} if message_metadata else {}))
        else:
            revision = body.get("revision")
            if type(revision) is not int:
                raise ValueError("input revision required")
            item = ctx.input_queue.get(identity)
            if item is None:
                return 404, {"ok": False, "error": "input_not_found"}
            if action == "edit":
                text = body.get("text")
                if not isinstance(text, str):
                    raise ValueError("input text required")
                item = ctx.input_queue.edit(identity, revision, text)
            elif action == "steer":
                item = ctx.input_queue.steer(identity, revision)
            elif action == "remove":
                item = ctx.input_queue.remove(identity, revision)
            elif action == "resume":
                if ctx.active_run_for(session_id=str(item["request"].get("session_id") or "")) is not None:
                    return 409, {"ok": False, "error": "task still stopping"}
                with ctx.task_runtime.spawn_line():
                    if _draining(ctx):
                        return 503, {"ok": False, "error": "server_draining"}
                    item = ctx.input_queue.resume(identity, revision)
                    ctx.run_registry.clear_stop(str(item["request"].get("run_id") or ""))
            else:
                raise ValueError("invalid input action")
        return 202, {"ok": True, "item": item}
    except ValueError as exc:
        return 409, {"ok": False, "error": str(exc)}
