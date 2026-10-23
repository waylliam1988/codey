"""Canonical passive conversation configuration and its disposable UI index."""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

from codey.control.protocol import Actor, ControlError, fingerprint
from codey.durable import control


def configurations(ctx: Any) -> list[dict[str, Any]]:
    return [config for key in ctx.durable.conversation_ids()
            if (config := control.latest(ctx.durable, key)) is not None]


def publish_inventory(ctx: Any) -> None:
    configs = configurations(ctx)
    with ctx.ui_state_store_lock:
        state = copy.deepcopy(ctx.ui_state_store.load())
        sessions = {item["id"]: item for item in state["sessions"]}
        projects = {item["path"]: item for item in state["projects"]}
        for config in configs:
            key = config["conversation_id"]
            project = config.get("project") or ""
            if project and project not in projects:
                projects[project] = {"id": "project_" + fingerprint(project)[:20], "name": Path(project).name,
                                     "path": project, "expanded": True, "createdAt": config["created_at"] * 1000}
            item = sessions.setdefault(key, {"id": key, "messages": [], "draft": {"text": ""},
                "title": config["title"], "provider": config["provider"],
                "projectId": projects[project]["id"] if project else None,
                "modelSelection": dict(config.get("model_selection") or {})})
            # Execution configuration seeds a missing sidebar entry. Existing
            # user preferences belong to the UI store and survive reconstruction.
            if item.get("modelSelection"):
                item["modelSelection"]["connection_id"] = item["provider"]
        state["sessions"] = list(sessions.values())
        state["projects"] = list(projects.values())
        state["updated_at"] = max(state.get("updated_at", 0), int(time.time() * 1000))
        ctx.ui_state_store.save(state, base_revision=state["revision"])
    ctx.emit({"type": "conversation_inventory_changed"})


def import_ui(ctx: Any) -> None:
    """Idempotently adopt configured UI sessions without changing history."""
    state = ctx.load_ui_state()
    projects = {item["id"]: item["path"] for item in state["projects"]}
    with ctx.durable.control_line():
        for item in state["sessions"]:
            # An unconfigured empty chat has no execution configuration to import.
            if not item.get("provider"):
                continue
            if control.latest(ctx.durable, item["id"]) is not None:
                continue
            config = {"conversation_id": item["id"], "title": item.get("title") or "Chat",
                      "provider": item["provider"], "project": projects.get(item.get("projectId"), ""),
                      "model_selection": item.get("modelSelection", {}), "created_at": time.time(),
                      "mode": "manual", "controller_actor_id": "", "created_by": "user", "control_generation": 0,
                      "expires_at": 0, "max_revisions": 3, "root_request_id": "", "max_seconds": 1800}
            control.write(ctx.durable, item["id"], "user", "import_ui", {"request_id": "import-ui"},
                          {"conversation_id": item["id"]}, config=config)


def config_for(ctx: Any, actor: Actor, conversation_id: str) -> dict[str, Any]:
    config = control.latest(ctx.durable, conversation_id)
    if config is None:
        raise ControlError("NOT_FOUND", "Conversation not found")
    if not actor.operator and actor.actor_id not in {config["created_by"], config["controller_actor_id"]}:
        raise ControlError("FORBIDDEN", "Conversation is outside this control identity")
    return config
