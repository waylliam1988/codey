from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from codey.agents.request import DEFAULT_MAX_TURNS
from codey.app import services
from codey.ghost.control_surface import GhostControlSurface
from codey.knowledge.concepts import ConceptGraphBuilder
from codey.knowledge.unified_graph import UnifiedResearchGraphBuilder
from codey.providers import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.providers.local_openai import (
    load_local_config,
    local_config_payload,
    probe_local_endpoint,
    save_local_config,
)
from codey.runs.details import load_run_details
from codey.workspace.changes import collect_changes, is_git_repository, restore_snapshot_changes


def query_list(query: dict[str, list[str]], key: str) -> list[str]:
    values: list[str] = []
    for raw in query.get(key, []):
        for item in str(raw or "").split(","):
            text = item.strip()
            if text and text not in values:
                values.append(text)
    return values


def query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int((query.get(key) or [default])[0])
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def ui_state_response(ctx: Any) -> tuple[int, dict]:
    return 200, {"ok": True, "state": ctx.load_ui_state()}


def save_ui_state_response(ctx: Any, body: dict) -> tuple[int, dict]:
    state = body.get("state") if isinstance(body, dict) else None
    try:
        ctx.save_ui_state(state)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except OSError as exc:
        return 500, {"ok": False, "error": str(exc)}
    return 200, {"ok": True}


def providers_response(ctx: Any) -> tuple[int, dict]:
    try:
        statuses = services.provider_availability(ctx)
    except Exception:
        statuses = {}
    return 200, {
        "default": DEFAULT_PROVIDER_ID,
        "providers": services.provider_payload(statuses),
    }


def local_provider_response() -> tuple[int, dict]:
    return 200, {"ok": True, "local": local_config_payload()}


def save_local_provider_response(body: dict) -> tuple[int, dict]:
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    model = str(body.get("model") or "").strip()
    raw_api_key = body.get("api_key")
    api_key = str(raw_api_key).strip() if raw_api_key is not None else ""
    if not base_url:
        return 400, {"ok": False, "error": "base_url required"}
    previous = load_local_config()
    previous_base_url = str(previous.get("base_url") or "").rstrip("/")
    probe_key = api_key
    same_target = bool(previous_base_url) and base_url == previous_base_url
    target_changed = bool(previous_base_url) and base_url != previous_base_url
    if not probe_key and same_target:
        probe_key = str(previous.get("api_key") or "")
    if not api_key and target_changed:
        return 400, {
            "ok": False,
            "error": "api_key required when base_url changes",
        }
    endpoint = probe_local_endpoint(base_url, api_key=probe_key)
    if endpoint is None:
        return 400, {"ok": False, "error": "could not reach an OpenAI-compatible /models endpoint"}
    try:
        save_local_config(
            endpoint.base_url,
            model or endpoint.default_model,
            None if same_target and not api_key else api_key,
        )
    except (OSError, ValueError) as exc:
        return 500, {"ok": False, "error": str(exc)}
    return 200, {"ok": True, "local": local_config_payload()}


def research_unconfigured_response() -> tuple[int, dict]:
    return 404, {"ok": False, "error": "Research is not configured"}


def research_graph_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict]:
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    focus_ids = query_list(query, "focus")
    synthesis_id = query_value(query, "synthesis_id")
    if synthesis_id and synthesis_id not in focus_ids:
        focus_ids.insert(0, synthesis_id)
    graph = UnifiedResearchGraphBuilder(ctx.knowledge_store).build_for_session(
        query_value(query, "session_id"),
        focus_ids=tuple(focus_ids),
        depth=query_int(query, "depth", 1, 1, 3),
        node_limit=query_int(query, "limit", 96, 8, 200),
        edge_limit=query_int(query, "edge_limit", 192, 8, 400),
        counterpoints=tuple(query_list(query, "counterpoint")[:8]),
    )
    return 200, {"ok": True, "graph": graph.to_dict()}


def research_concept_graph_response(
    ctx: Any,
    query: dict[str, list[str]],
) -> tuple[int, dict]:
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    graph = ConceptGraphBuilder(ctx.knowledge_store).build_for_session(
        query_value(query, "session_id"),
        node_limit=query_int(query, "limit", 64, 8, 200),
        edge_limit=query_int(query, "edge_limit", 128, 8, 400),
    )
    return 200, {"ok": True, "graph": graph.to_dict()}


def research_note_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict]:
    note_id = query_value(query, "id")
    if not note_id:
        return 400, {"ok": False, "error": "id required"}
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    note = ctx.knowledge_store.read_note(note_id)
    if note is None:
        return 404, {"ok": False, "error": "note not found"}
    row = ctx.knowledge_store.index.get(note.id) or {}
    return 200, {
        "ok": True,
        "note": {
            "id": note.id,
            "type": note.type,
            "title": note.title,
            "body": note.body,
            "sources": note.sources,
            "tags": note.tags,
            "status": note.status,
            "path": str(row.get("path") or ""),
            "updated": note.updated,
        },
    }


def run_details_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict]:
    session_id = str((query.get("session_id") or [""])[0] or "").strip()
    run_id = str((query.get("run_id") or [""])[0] or "").strip()
    if not session_id or not run_id:
        return 400, {"ok": False, "error": "session_id and run_id required"}
    summary = load_run_details(
        run_ledgers=ctx.run_ledgers,
        run_traces=ctx.run_traces,
        runtime_operations=ctx.runtime_operations,
        runtime_effects=getattr(ctx, "runtime_effects", None),
        tool_result_delivery=getattr(ctx, "tool_result_delivery", None),
        session_id=session_id,
        run_id=run_id,
    )
    return 200, {
        "ok": True,
        "available": summary.available,
        "details": summary.to_jsonable(),
    }


def research_restore_response(ctx: Any, body: dict) -> tuple[int, dict]:
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        return 400, {"ok": False, "error": "run_id required"}
    payload = ctx.restore_research_changes(run_id)
    return 200 if payload.get("ok") else 409, payload


def ghost_control_surface(ctx: Any) -> GhostControlSurface:
    return GhostControlSurface(
        inbox=ctx.ghost_inbox,
        hebbian=ctx.ghost_hebbian,
        continuity=ctx.ghost_continuity,
        router=ctx.ghost_router,
        sleep=ctx.ghost_sleep,
        work_queue=ctx.ghost_work_queue,
        affinity=ctx.ghost_affinity,
        signals=ctx.ghost_signals,
    )


def ghost_summary_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict]:
    payload = ghost_control_surface(ctx).summary(
        session_id=query_value(query, "session_id"),
        project=query_value(query, "project"),
    )
    return 200, payload


def ghost_export_response(ctx: Any) -> tuple[int, dict]:
    return 200, ghost_control_surface(ctx).export_state()


def ghost_action_response(ctx: Any, body: dict) -> tuple[int, dict]:
    return ghost_control_surface(ctx).dispatch_action(body)


def changes_response(ctx: Any, project: str) -> tuple[int, dict]:
    key = str(Path(project).expanduser().resolve()) if project else ""
    tracker = (
        ctx.change_tracker_for(key, persistent=not is_git_repository(key))
        if key
        else None
    )
    payload = collect_changes(project, tracker)
    return 200 if payload.get("ok") else 400, payload


def restore_changes_response(ctx: Any, body: dict) -> tuple[int, dict]:
    project = (body.get("project") or "").strip()
    if not project:
        return 400, {"ok": False, "error": "project required"}
    paths = body.get("paths")
    if paths is not None and not isinstance(paths, list):
        return 400, {"ok": False, "error": "paths must be a list"}
    clean_paths = [str(path) for path in paths] if paths is not None else None
    key = str(Path(project).expanduser().resolve())
    if ctx.has_active_run_for_project(key):
        return 409, {"ok": False, "error": "run in progress"}
    tracker = ctx.change_tracker_for(
        key,
        persistent=not is_git_repository(key),
    )
    if not tracker.has_snapshots:
        tracker = None
    return restore_snapshot_changes(project, tracker, clean_paths)


def run_submit_response(
    body: dict,
    submit_task: Callable[..., str | None],
) -> tuple[int, dict]:
    session_id = str(body.get("session_id") or "").strip() or "default"
    project = (body.get("project") or "").strip() or None
    task = (body.get("task") or "").strip()
    continue_task = bool(body.get("continue_task"))
    provider_id = str(body.get("provider") or DEFAULT_PROVIDER_ID).strip().lower()
    intent = str(body.get("intent") or "auto").strip().lower()
    if intent not in {
        "auto",
        "chat",
        "research",
        "project",
        "hybrid",
        "planning_readonly",
        "readonly",
        "planning",
        "review",
    }:
        return 400, {"error": "invalid intent"}
    try:
        max_turns = int(body.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        return 400, {"error": "invalid max_turns"}
    max_turns = max(1, min(max_turns, 500))
    if not task:
        return 400, {"error": "task required"}
    if provider_id not in PROVIDER_LABELS:
        return 400, {"error": f"unsupported provider: {provider_id}"}
    if intent == "review" and not project:
        return 400, {"error": "project required for review"}
    if project:
        Path(project).mkdir(parents=True, exist_ok=True)
    try:
        run_id = submit_task(
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
        )
    except Exception as exc:
        return 500, {"error": str(exc)}
    if run_id is None:
        return 409, {"error": "busy", "hint": "try_continue"}
    return 200, {"ok": True, "run_id": run_id}


def shell_approval_response(
    ctx: Any,
    body: dict,
    *,
    submit_task_after_slot_release: Callable[..., str | None],
) -> tuple[int, dict]:
    approval_id = str(body.get("id") or "").strip()
    approved = body.get("approved") is True
    pending = ctx.pop_pending_shell_approval(approval_id)
    if not pending:
        return 404, {"error": "approval not found"}
    session_id = pending["session_id"]
    command = pending["command"]
    if not approved:
        event = {
            "type": "shell_result",
            "run_id": pending.get("run_id") or "",
            "session_id": session_id,
            "id": approval_id,
            "approved": False,
            "command": command,
            "cwd": pending["cwd"],
            "output": "用户已拒绝执行该命令。",
            "exit_code": None,
        }
        ctx.record_shell_result(event)
        return 200, {"ok": True, "approved": False, "event": event}

    result = services.execute_approved_shell(ctx, pending["project"], pending["cwd"], command)
    event = {
        "type": "shell_result",
        "run_id": pending.get("run_id") or "",
        "session_id": session_id,
        "id": approval_id,
        "approved": True,
        "command": command,
        "cwd": pending["cwd"],
        "output": result.get("output") or result.get("error") or "",
        "exit_code": result.get("exit_code"),
        "ok": result.get("ok"),
        "truncated": bool(result.get("truncated")),
    }
    ctx.record_shell_result(event)
    continued = False
    continuation_stopped = False
    if pending.get("continue_after"):
        if ctx.run_registry.stop_flag.is_set():
            continuation_stopped = True
        else:
            setup_context = services.shell_continuation_setup_context(pending)
            followup_hints = services.shell_followup_hints(
                pending=pending,
                result=result,
            )
            continuation = services.build_shell_approval_continuation(
                command=command,
                result=result,
                post_approval_instructions=str(
                    pending.get("post_approval_instructions") or ""
                ),
                setup_context=setup_context,
                followup_hints=followup_hints,
            )
            active = ctx.current_run()
            active_provider = (
                active.provider_id
                if active is not None
                and active.run_id == str(pending.get("run_id") or "")
                and active.session_id == session_id
                else ""
            )
            provider_id = active_provider or pending.get("provider") or DEFAULT_PROVIDER_ID
            continuation_run = submit_task_after_slot_release(
                session_id,
                pending["project"],
                continuation,
                int(pending["max_turns"]),
                True,
                provider_id,
                "project",
                previous_run_id=str(pending.get("run_id") or ""),
            )
            continued = continuation_run is not None
            continuation_stopped = not continued and ctx.run_registry.stop_flag.is_set()
    return 200, {
        "ok": True,
        "approved": True,
        "continued": continued,
        "stopped": continuation_stopped,
        "result": result,
        "event": event,
    }


def teach_resume_response(ctx: Any, body: dict) -> tuple[int, dict]:
    teach_id = str(body.get("id") or "").strip()
    if not ctx.resume_pending_teach(teach_id):
        return 404, {"error": "pause not found"}
    return 200, {"ok": True}


def new_chat_response(ctx: Any, body: dict) -> tuple[int, dict]:
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return 400, {"ok": False, "error": "session_id required"}
    if ctx.active_run_for(session_id=session_id) is not None:
        return 409, {"ok": False, "error": "run in progress"}
    failures = ctx.forget_conversation(session_id)
    if failures:
        return 200, {
            "ok": True,
            "warnings": [f"Failed to purge store {k}: {v}" for k, v in failures.items()],
            "unpurged_stores": list(failures.keys()),
        }
    return 200, {"ok": True}


def stop_response(ctx: Any) -> tuple[int, dict]:
    ctx.run_registry.stop_flag.set()
    ctx.cancel_pending_teach()
    ctx.expire_pending_shell_approvals()
    return 200, {"ok": True}


__all__ = [
    "changes_response",
    "ghost_action_response",
    "ghost_export_response",
    "ghost_summary_response",
    "local_provider_response",
    "new_chat_response",
    "providers_response",
    "research_concept_graph_response",
    "research_graph_response",
    "research_note_response",
    "research_restore_response",
    "restore_changes_response",
    "run_details_response",
    "run_submit_response",
    "save_local_provider_response",
    "save_ui_state_response",
    "shell_approval_response",
    "stop_response",
    "teach_resume_response",
    "ui_state_response",
]
