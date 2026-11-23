"""Canonical passive conversation configuration and its disposable UI index."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import TYPE_CHECKING

from codey.control.protocol import Actor, ControlError, fingerprint
from codey.durable import control
from codey.types.json_value import require_json_integer, require_json_string, require_json_strings
from codey.utils.object_fields import require_object_fields, require_object_rows

if TYPE_CHECKING:
    from codey.app.context import AppContext


def configurations(ctx: AppContext) -> list[dict[str, object]]:
    configs: list[dict[str, object]] = []
    for key in ctx.durable.conversation_ids():
        config: object = control.latest(ctx.durable, key)
        if config is not None:
            configs.append(require_object_fields(config))
    return configs


def publish_inventory(ctx: AppContext) -> None:
    configs = configurations(ctx)
    with ctx.ui_state_store_lock:
        raw_state: object = ctx.ui_state_store.load()
        state = copy.deepcopy(require_object_fields(raw_state))
        sessions = {require_json_string(item["id"], "session ID"): item for item in require_object_rows(state["sessions"])}
        projects = {require_json_string(item["path"], "project path"): item for item in require_object_rows(state["projects"])}
        deleted_ids = require_object_fields(state.get("deleted_ids", dict[str, object]()))
        deleted = {kind: set(require_json_strings(deleted_ids.get(kind, list[str]()), kind))
                   for kind in ("sessions", "projects")}
        for config in configs:
            key = require_json_string(config["conversation_id"], "conversation ID")
            # Retained execution history must not recreate deleted UI entries.
            if key in deleted["sessions"]:
                continue
            project = require_json_string(config.get("project") or "", "project path")
            project_id = "project_" + fingerprint(project)[:20]
            if project and project not in projects and project_id not in deleted["projects"]:
                created_at = config["created_at"]
                if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
                    raise ValueError("conversation creation time must be a number")
                projects[project] = {"id": project_id, "name": Path(project).name,
                                     "path": project, "expanded": True, "createdAt": created_at * 1000}
            item = sessions.setdefault(key, {"id": key, "messages": list[dict[str, object]](), "draft": {"text": ""},
                "title": config["title"], "provider": config["provider"],
                "projectId": projects[project]["id"] if project in projects else None,
                "modelSelection": dict(require_object_fields(config.get("model_selection") or dict[str, object]()))})
            # Execution configuration seeds a missing sidebar entry. Existing
            # user preferences belong to the UI store and survive reconstruction.
            if item.get("modelSelection"):
                require_object_fields(item["modelSelection"])["connection_id"] = item["provider"]
        state["sessions"] = list(sessions.values())
        state["projects"] = list(projects.values())
        state["updated_at"] = max(require_json_integer(state.get("updated_at", 0), "updated time"), int(time.time() * 1000))
        _saved: object = ctx.ui_state_store.save(state, base_revision=require_json_integer(state["revision"], "UI revision"))
    ctx.emit({"type": "conversation_inventory_changed"})


def import_ui(ctx: AppContext) -> None:
    """Idempotently adopt configured UI sessions without changing history."""
    state = ctx.load_ui_state()
    projects = {require_json_string(item["id"], "project ID"): require_json_string(item["path"], "project path")
                for item in require_object_rows(state["projects"])}
    with ctx.durable.control_line():
        for item in require_object_rows(state["sessions"]):
            # An unconfigured empty chat has no execution configuration to import.
            if not item.get("provider"):
                continue
            identity = require_json_string(item["id"], "session ID")
            existing: object = control.latest(ctx.durable, identity)
            if existing is not None:
                continue
            project_id = item.get("projectId")
            config: dict[str, object] = {"conversation_id": identity, "title": item.get("title") or "Chat",
                      "provider": item["provider"], "project": projects.get(project_id if isinstance(project_id, str) else "", ""),
                      "model_selection": item.get("modelSelection", dict[str, object]()), "created_at": time.time(),
                      "mode": "manual", "controller_actor_id": "", "created_by": "user", "control_generation": 0,
                      "expires_at": 0, "max_revisions": 3, "root_request_id": "", "max_seconds": 1800}
            params: dict[str, object] = {"request_id": "import-ui"}
            result: dict[str, object] = {"conversation_id": identity}
            control.write(ctx.durable, identity, "user", "import_ui", params, result, config=config)


def config_for(ctx: AppContext, actor: Actor, conversation_id: str) -> dict[str, object]:
    raw_config: object = control.latest(ctx.durable, conversation_id)
    if raw_config is None:
        raise ControlError("NOT_FOUND", "Conversation not found")
    config = require_object_fields(raw_config)
    if not actor.operator and actor.actor_id not in {
        require_json_string(config["created_by"], "creator"), require_json_string(config["controller_actor_id"], "controller"),
    }:
        raise ControlError("FORBIDDEN", "Conversation is outside this control identity")
    return config
