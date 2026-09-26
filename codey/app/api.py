from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from codey.agents.request import DEFAULT_MAX_TURNS
from codey.agents.shell_approval import shell_command_event_fields
from codey.app import provider_services, shell_service
from codey.automation.browser_worker import BrowserWorkerBusy
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV
from codey.ghost.control_surface import GhostControlSurface
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.providers.local_config import (
    LocalProviderConfig,
    assemble_bootstrap_payload,
    load_local_config,
    local_bootstrap_payload,
    parse_local_config_update,
    resolve_local_context_budget,
    save_local_config,
    select_local_target,
)
from codey.providers.local_discovery import LocalEndpoint, probe_local_endpoint_detail
from codey.runs.details import load_run_details
from codey.storage.local_store import StoreCorruption
from codey.workspace.changes import collect_changes, is_git_repository, restore_snapshot_changes


def build_unified_research_graph(*args: object, **kwargs: object) -> object:
    """Lazy facade: importing api must not load the knowledge graph stack.

    Keeps its historical module-attribute name so existing
    ``mock.patch.object(app_api, ...)`` doubles keep working.
    """
    from codey.knowledge.concepts import build_unified_research_graph as _build

    return _build(*args, **kwargs)


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
    base_revision = body.get("base_revision") if isinstance(body, dict) else None
    if isinstance(base_revision, bool) or not isinstance(base_revision, int):
        try:
            base_revision = int(base_revision or 0)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "base_revision required"}
    try:
        saved = ctx.save_ui_state(state, base_revision=int(base_revision))
    except Exception as exc:
        from codey.storage.ui_state_store import UiStateConflict

        if isinstance(exc, UiStateConflict):
            return 409, {"ok": False, "error": "ui_state_conflict", "revision": exc.current_revision}
        if isinstance(exc, ValueError):
            return 400, {"ok": False, "error": str(exc)}
        if isinstance(exc, OSError):
            return 500, {"ok": False, "error": str(exc)}
        raise
    return 200, {"ok": True, "revision": int(saved.get("revision") or 0)}


def _project_directory_error(project: str | None) -> str:
    if not project:
        return ""
    try:
        if Path(project).expanduser().is_dir():
            return ""
    except (OSError, RuntimeError, ValueError):
        pass
    return "project not found, use pick_folder"


logger = logging.getLogger(__name__)


def providers_response(ctx: Any) -> tuple[int, dict]:
    try:
        statuses = provider_services.provider_availability(ctx)
    except Exception:
        logger.exception("provider availability probe failed")
        statuses = {}
        probe_error = True
    else:
        probe_error = False
    return 200, {
        "default": DEFAULT_PROVIDER_ID,
        "recommended": provider_services.recommended_default_provider(statuses),
        "providers": provider_services.provider_payload(statuses),
        "probe_error": probe_error,
    }


def provider_catalog_response() -> tuple[int, dict]:
    """Boot-time catalog: static ids + labels, never runs the availability probe."""
    return 200, {
        "default": DEFAULT_PROVIDER_ID,
        "providers": provider_services.provider_catalog(),
    }


def local_provider_response() -> tuple[int, dict]:
    return 200, {"ok": True, "local": local_bootstrap_payload()}


def save_local_provider_response(body: dict) -> tuple[int, dict]:
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "request body must be an object"}
    previous = load_local_config()
    parsed, error = parse_local_config_update(body, previous)
    if error or parsed is None:
        return 400, {"ok": False, "error": error or "invalid local provider update"}
    try:
        resolve_local_context_budget(parsed)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    # Runtime is env-controlled when an env base is set: refuse to validate
    # a different form address as if it were the running target.
    env_base = os.environ.get(LOCAL_OPENAI_BASE_URL_ENV, "").strip().rstrip("/")
    if env_base and env_base.rstrip("/") != parsed.base_url.rstrip("/"):
        return 400, {
            "ok": False,
            "error": (
                f"LOCAL_OPENAI_BASE_URL is set to {env_base} and controls the runtime address; "
                f"clear it to save {parsed.base_url}"
            ),
        }
    previous_base_url = previous.base_url.rstrip("/")
    same_target = bool(previous_base_url) and parsed.base_url.rstrip("/") == previous_base_url.rstrip("/")
    # Same target with no key keeps the previous key for probing; a new
    # address never inherits the old key (empty probes as empty).
    probe_api_key = parsed.api_key if parsed.api_key or not same_target else previous.api_key
    probe_selection = select_local_target(LocalProviderConfig(
        base_url=parsed.base_url,
        model=parsed.model,
        api_key=probe_api_key,
        native_tools_mode=parsed.native_tools_mode,
        context=parsed.context,
    ))
    # One probe only: branching on a single detail result keeps the verdict
    # consistent even if the endpoint flaps between two requests.
    endpoint, reason = probe_local_endpoint_detail(
        probe_selection.base_url, api_key=probe_selection.api_key,
    )
    if endpoint is None or reason != "ok":
        detail = {
            "unreachable": "could not reach an OpenAI-compatible /models endpoint",
            "auth": "local endpoint rejected the api_key (401/403)",
            "invalid_json": "local endpoint returned a non-OpenAI /models payload",
        }.get(reason, "could not reach an OpenAI-compatible /models endpoint")
        return 400, {"ok": False, "error": detail, "reason": reason}
    try:
        saved = LocalProviderConfig(
            base_url=endpoint.base_url,
            model=parsed.model or endpoint.default_model,
            api_key=previous.api_key if same_target and not parsed.api_key else parsed.api_key,
            native_tools_mode=parsed.native_tools_mode,
            context=parsed.context,
        )
        save_local_config(saved)
    except (OSError, ValueError) as exc:
        return 500, {"ok": False, "error": str(exc)}
    # No second probe: assemble the status from the endpoint just verified
    # above, with the saved model first like the remembered-target display.
    saved_selection = select_local_target(saved)
    wanted = (saved_selection.model or endpoint.default_model or "").strip()
    ordered = LocalEndpoint(
        endpoint.base_url,
        ((wanted,) if wanted else ()) + tuple(m for m in endpoint.models if m != wanted),
    )
    return 200, {"ok": True, "local": assemble_bootstrap_payload(saved, saved_selection, ordered, [])}


def research_unconfigured_response() -> tuple[int, dict]:
    return 404, {"ok": False, "error": "Research is not configured"}


def research_graph_response(ctx: Any, query: dict[str, list[str]]) -> tuple[int, dict]:
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    focus_ids = query_list(query, "focus")
    synthesis_id = query_value(query, "synthesis_id")
    if synthesis_id and synthesis_id not in focus_ids:
        focus_ids.insert(0, synthesis_id)
    graph = build_unified_research_graph(
        ctx.knowledge_store,
        query_value(query, "session_id"),
        focus_ids=tuple(focus_ids),
        depth=query_int(query, "depth", 1, 1, 3),
        node_limit=query_int(query, "limit", 96, 8, 200),
        edge_limit=query_int(query, "edge_limit", 192, 8, 400),
        counterpoints=tuple(query_list(query, "counterpoint")[:8]),
    )
    return 200, {"ok": True, "graph": graph.to_dict()}


def research_notes_response(ctx: Any, body: dict) -> tuple[int, dict]:
    raw_ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        return 400, {"ok": False, "error": "ids required"}
    seen: list[str] = []
    for raw in raw_ids[:64]:
        text = str(raw or "").strip()
        if text and text not in seen:
            seen.append(text)
    if not seen:
        return 400, {"ok": False, "error": "ids required"}
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    notes: dict[str, dict] = {}
    missing: list[str] = []
    for note_id, (note, row) in ctx.knowledge_store.read_notes_with_rows(seen).items():
        notes[note_id] = _research_note_payload(ctx, note, row)
    for note_id in seen:
        if note_id not in notes:
            missing.append(note_id)
    return 200, {"ok": True, "notes": notes, "missing": missing}


def _research_note_payload(ctx: Any, note: Any, row: dict) -> dict:
    return {
        "id": note.id,
        "type": note.type,
        "title": note.title,
        "body": note.body,
        "sources": note.sources,
        "tags": note.tags,
        "status": note.status,
        "path": str(row.get("path") or ""),
        "updated": note.updated,
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
        observations=ctx.ghost_observations,
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


def changes_response(ctx: Any, project: object) -> tuple[int, dict]:
    project_text = str(project or "").strip()
    key = str(Path(project_text).expanduser().resolve()) if project_text else ""
    try:
        tracker = (
            ctx.change_tracker_for(key, persistent=not is_git_repository(key))
            if key
            else None
        )
        payload = collect_changes(project_text, tracker)
    except StoreCorruption:
        payload = {
            "ok": False,
            "error": "snapshot needs repair",
            "files": [],
            "diff": "",
        }
    return 200 if payload.get("ok") else 400, payload


def restore_changes_response(ctx: Any, body: dict) -> tuple[int, dict]:
    project = str((body.get("project") if isinstance(body, dict) else "") or "").strip()
    if not project:
        return 400, {"ok": False, "error": "project required"}
    paths = body.get("paths")
    if paths is not None and not isinstance(paths, list):
        return 400, {"ok": False, "error": "paths must be a list"}
    clean_paths = [str(path) for path in paths] if paths is not None else None
    key = str(Path(project).expanduser().resolve())
    if ctx.has_active_run_for_project(key):
        return 409, {"ok": False, "error": "run in progress"}
    # Single-writer promise covers tasks and user restore: hold the same
    # project writer lease from reading the recovery basis to finishing
    # file writes. Cross-process contention fails fast with 409.
    acquired = bool(ctx.acquire_project_writer(key))
    try:
        if not acquired:
            return 409, {"ok": False, "error": "project writer busy"}
        tracker = ctx.change_tracker_for(
            key,
            persistent=not is_git_repository(key),
        )
        if not tracker.has_snapshots:
            tracker = None
        return restore_snapshot_changes(project, tracker, clean_paths)
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                ctx.release_project_writer(key)


def run_submit_response(
    body: dict,
    submit_task: Callable[..., str | None],
) -> tuple[int, dict]:
    if not isinstance(body, dict):
        return 400, {"error": "invalid json"}
    session_id = str(body.get("session_id") or "").strip() or "default"
    project = str(body.get("project") or "").strip() or None
    task = str(body.get("task") or "").strip()
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
    project_error = _project_directory_error(project)
    if project_error:
        return 400, {"error": project_error}
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
    except BrowserWorkerBusy:
        return 503, {"error": "browser worker busy", "hint": "retry"}
    except Exception as exc:
        return 500, {"error": str(exc)}
    if run_id is None:
        return 409, {"error": "busy", "hint": "try_continue"}
    return 200, {"ok": True, "run_id": run_id}


def _stopped_shell_denial(
    ctx: Any, pending: dict, approval_id: str, session_id: str, command: object
) -> tuple[int, dict]:
    event = {
        "type": "shell_result",
        "run_id": pending.get("run_id") or "",
        "session_id": session_id,
        "id": approval_id,
        "approved": False,
        "status": "stopped",
        **shell_command_event_fields(pending or {"command": command}),
        "cwd": pending["cwd"],
        "output": "Task stopped; command approval expired.",
        "exit_code": None,
    }
    ctx.record_shell_result(event)
    return 409, {"error": "stopped", "stopped": True, "status": "stopped", "event": event}


def shell_approval_response(
    ctx: Any,
    body: dict,
    *,
    submit_task_after_slot_release: Callable[..., str | None],
    continuation_retry_after: int = 15,
) -> tuple[int, dict]:
    approval_id = str(body.get("id") or "").strip()
    approved = body.get("approved") is True
    if not approved:
        pending = ctx.pop_pending_shell_approval(approval_id)
        if not pending:
            return 404, {"error": "approval not found"}
        pending.pop("_approval_generation", None)
        session_id = pending["session_id"]
        command = pending["command"]
        event = {
            "type": "shell_result",
            "run_id": pending.get("run_id") or "",
            "session_id": session_id,
            "id": approval_id,
            "approved": False,
            **shell_command_event_fields(pending),
            "cwd": pending["cwd"],
            "output": "Denied by user.",
            "exit_code": None,
        }
        ctx.record_shell_result(event)
        return 200, {"ok": True, "approved": False, "event": event}

    # Approved path: atomic claim (pop + generation + stop + cwd) under one
    # lock hold, then ticket-only execution with a spawn gate before Popen.
    # Internal epoch, never leaked to events/UI.
    from codey.policies.limits import SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT

    pending, ticket = shell_service.claim_shell_ticket(
        ctx,
        approval_id,
        timeout=SHELL_TIMEOUT,
        output_limit=SHELL_OUTPUT_LIMIT,
    )
    if pending is None:
        return 404, {"error": "approval not found"}
    project = str(pending.get("project") or "").strip()
    max_turns = int(pending.get("max_turns") or DEFAULT_MAX_TURNS)
    session_id = pending["session_id"]
    command = pending["command"]
    if ticket is None:
        return _stopped_shell_denial(ctx, pending, approval_id, session_id, command)

    result = shell_service.execute_shell_ticket(ctx, ticket)
    if result.get("status") == "stopped" or result.get("stopped"):
        return _stopped_shell_denial(ctx, pending, approval_id, session_id, command)
    event = {
        "type": "shell_result",
        "run_id": pending.get("run_id") or "",
        "session_id": session_id,
        "id": approval_id,
        "approved": True,
        "status": str(result.get("status") or "exit"),
        **shell_command_event_fields(pending or {"command": command}),
        "cwd": pending["cwd"],
        "output": result.get("output") or result.get("error") or "",
        "exit_code": result.get("exit_code"),
        "ok": result.get("ok"),
        "truncated": bool(result.get("truncated")),
    }
    ctx.record_shell_result(event)
    continued = False
    continuation_stopped = False
    continuation_requested = bool(pending.get("continue_after"))
    if continuation_requested:
        if ctx.run_registry.stop_flag.is_set():
            continuation_stopped = True
        else:
            continuation_plan = shell_service.build_shell_approval_continuation_plan(
                pending=pending,
                result=result,
                active_run=ctx.current_run(),
            )
            continuation_run = submit_task_after_slot_release(
                session_id,
                project or None,
                continuation_plan.continuation,
                max_turns,
                True,
                continuation_plan.provider_id,
                "project",
                previous_run_id=str(pending.get("run_id") or ""),
            )
            continued = continuation_run is not None
            continuation_stopped = not continued and ctx.run_registry.stop_flag.is_set()
    payload = {
        "ok": True,
        "approved": True,
        "continued": continued,
        "continuation_requested": continuation_requested,
        "stopped": continuation_stopped,
        "result": result,
        "event": event,
    }
    if continuation_requested and not continued and not continuation_stopped:
        payload["retry_after"] = continuation_retry_after
    return 200, payload


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
    ctx.request_stop()
    return 200, {"ok": True}


__all__ = [
    "changes_response",
    "ghost_action_response",
    "ghost_export_response",
    "ghost_summary_response",
    "local_provider_response",
    "new_chat_response",
    "provider_catalog_response",
    "providers_response",
    "research_graph_response",
    "research_notes_response",
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
