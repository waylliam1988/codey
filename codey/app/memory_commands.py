"""Canonical user memory commands, serialized by the existing durable owner."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from codey.durable.model import EntryDraft, thaw_json
from codey.memory.codec import unique_scopes
from codey.types.json_value import JsonObject, require_json_string
from codey.workspace.context_admission import request_scope

if TYPE_CHECKING:
    from codey.app.context import AppContext


def selected_scope(payload: JsonObject, session_id: str, project: str) -> str:
    kind = payload.get("scope_kind", "project" if project else "session")
    if kind == "user":
        return "user"
    if kind == "project" and project:
        return request_scope(project, "")
    if kind == "session" and session_id:
        return request_scope("", session_id)
    raise ValueError("scope_kind requires a user, an existing session or a project")


def control_scopes(project: str, session_id: str) -> tuple[str, ...]:
    return unique_scopes(("user", request_scope(project, session_id), request_scope("", session_id)))


def record_command(ctx: AppContext, session_id: str, kind: str, payload: JsonObject,
                   request_id: str = "", *, validate: Callable[[str], None] | None = None) -> EntryDraft:
    # A command may commit before its projection fails. Retrying a caller-owned
    # request ID must reuse exactly that canonical command and its timestamp.
    with ctx.durable.control_line():
        if request_id:
            prior = next((entry for entry in ctx.durable.entries(session_id)
                          if entry.kind == kind and entry.payload.get("request_id") == request_id), None)
            if prior is not None:
                if thaw_json(prior.payload.get("command")) != payload:
                    raise ValueError("request_id already names a different memory command")
                return prior
        at = ctx.memory.clock()
        if validate is not None:
            validate(at)
        return ctx.durable.append_message(session_id, kind, {
            "command": payload, "request_id": request_id, "occurred_at": at,
        })


def command_time(entry: EntryDraft) -> str:
    return require_json_string(entry.payload.get("occurred_at"), "occurred_at")


def command_ref(entry: EntryDraft) -> str:
    return f"management:{entry.conversation_id}:{entry.entry_id}"
