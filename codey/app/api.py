from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import Field, asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Protocol, TypeGuard

if TYPE_CHECKING:
    from codey.app.context import AppContext
    from codey.knowledge.store import NoteDocumentMetadata
    from codey.reviews.core import ReviewResult

from codey.agents.request import DEFAULT_MAX_TURNS
from codey.app import provider_services
from codey.app.http_response import response_fields as _response_fields
from codey.app.http_response import response_pair as _response_pair
from codey.app.memory_routes import memory_action, memory_summary
from codey.app.submission_types import ContinuationSubmitter, TaskSubmitOptions, TaskSubmitter
from codey.automation.browser_worker import BrowserWorkerBusy
from codey.durable.errors import CommitConflict, IdempotencyConflict
from codey.env_names import LOCAL_OPENAI_BASE_URL_ENV
from codey.knowledge.note import KnowledgeNote
from codey.policies.output_requirement import submission_fields
from codey.providers.catalog import API_CONNECTIONS, DEFAULT_PROVIDER_ID, PROVIDER_LABELS
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
from codey.providers.model_preferences import ModelDisabledError
from codey.runs.details import load_run_details
from codey.runtime.core.api_selection import ApiRunSelection
from codey.storage.local_store import StoreCorruption
from codey.task.entry_auth import EntryAuth, derive_entry_auth
from codey.task.model import EditUserMessage, HandoffRun, ModelHandoffRejected, ResumeRun, StartTask
from codey.utils.object_fields import object_fields, sequence_items
from codey.utils.refs import coerce_int
from codey.workspace.changes import (
    ChangeTracker,
    collect_changes,
    collect_git_file_diff,
    is_git_repository,
    restore_snapshot_changes,
)

_SubmissionArgs = tuple[str, str | None, str, int, bool, str, str]
_CLASS_TYPE: type[object] = type


class _DataclassInstance(Protocol):
    __dataclass_fields__: ClassVar[dict[str, Field[object]]]


def _local_bootstrap(*, probe: bool) -> dict[str, object]:
    value: object = local_bootstrap_payload(probe=probe)
    return _response_fields(value)


def _is_dataclass_instance(value: object) -> TypeGuard[_DataclassInstance]:
    return is_dataclass(value) and not isinstance(value, _CLASS_TYPE)


def _dataclass_fields(value: object) -> dict[str, object]:
    if not _is_dataclass_instance(value):
        raise TypeError("response value must be a dataclass instance")
    fields: object = asdict(value)
    return _response_fields(fields)


def _is_review_result(value: object) -> TypeGuard[ReviewResult]:
    from codey.reviews.core import ReviewResult

    result_type: type[object] = ReviewResult
    return isinstance(value, result_type)


def _pending_question(ctx: AppContext | None, session_id: str) -> dict[str, object] | None:
    if ctx is None:
        return None
    value: object = ctx.user_questions.pending(session_id)
    return _response_fields(value) if value is not None else None


def query_list(query: Mapping[str, Sequence[object]], key: str) -> list[str]:
    values: list[str] = []
    for raw in query.get(key, []):
        for item in str(raw or "").split(","):
            text = item.strip()
            if text and text not in values:
                values.append(text)
    return values


def query_int(
    query: Mapping[str, Sequence[object]],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = (query.get(key) or [str(default)])[0]
    if isinstance(raw, bool):
        value = default
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isascii() and raw.strip().isdigit():
        try:
            value = int(raw.strip())
        except (TypeError, ValueError, OverflowError):
            value = default
    else:
        value = default
    return max(minimum, min(maximum, value))


def query_value(query: Mapping[str, Sequence[object]], key: str) -> str:
    raw = (query.get(key) or [""])[0]
    if raw is None or isinstance(raw, bool):
        return ""
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def ui_state_response(ctx: AppContext) -> tuple[int, dict[str, object]]:
    return 200, {"ok": True, "state": ctx.load_ui_state()}


def save_ui_state_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    state = body.get("state") if isinstance(body, dict) else None
    base_revision = body.get("base_revision") if isinstance(body, dict) else None
    if type(base_revision) is not int:
        return 400, {"ok": False, "error": "base_revision required"}
    try:
        saved = ctx.save_ui_state(state, base_revision=base_revision)
    except Exception as exc:
        from codey.storage.ui_state_store import UiStateConflict

        if isinstance(exc, UiStateConflict):
            return 409, {"ok": False, "error": "ui_state_conflict", "revision": exc.current_revision}
        if isinstance(exc, ValueError):
            return 400, {"ok": False, "error": str(exc)}
        if isinstance(exc, OSError):
            return 500, {"ok": False, "error": str(exc)}
        raise
    return 200, {"ok": True, "revision": coerce_int(saved.get("revision") or 0)}


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


def providers_response(ctx: AppContext) -> tuple[int, dict[str, object]]:
    state_home: object = getattr(ctx, "state_home", None)
    try:
        ctx._provider_status_snapshot_requested = True
        legacy_statuses = (
            provider_services.provider_availability(ctx)
            if state_home is None else None
        )
        provider_rows = (
            provider_services.provider_payload(legacy_statuses)
            if legacy_statuses is not None
            else provider_services.provider_status_payload(ctx)
        )
    except Exception:
        logger.exception("provider availability probe failed")
        provider_rows = provider_services.provider_payload({})
        probe_error = True
    else:
        probe_error = False
    return 200, {
        "default": DEFAULT_PROVIDER_ID,
        "providers": provider_rows,
        "probe_error": probe_error,
    }


def provider_catalog_response() -> tuple[int, dict[str, object]]:
    """Boot-time catalog: static ids + labels, never runs the availability probe."""
    return 200, {
        "default": DEFAULT_PROVIDER_ID,
        "providers": provider_services.provider_catalog(),
    }


def local_provider_response(ctx: AppContext | None = None) -> tuple[int, dict[str, object]]:
    from codey.app.model_settings import preferences_for

    coordinator = ctx.connection_status if ctx is not None else None
    if coordinator is not None and preferences_for(ctx).enabled("local"):
        force_probe = not bool(ctx._provider_status_snapshot_requested if ctx is not None else False)
        coordinator.refresh("local", force=force_probe)
        if force_probe:
            coordinator.wait_for("local", timeout=2.0)
        status_snapshot = coordinator.current("local")
        data: dict[str, object] = coordinator.data("local")
        cached_local = data.get("local")
        local = dict(cached_local) if object_fields(cached_local) else _local_bootstrap(probe=False)
        local.update({
            "state": status_snapshot.state,
            "freshness": status_snapshot.freshness,
            "source": status_snapshot.source,
            "status_error": status_snapshot.error,
        })
    else:
        local = _local_bootstrap(probe=preferences_for(ctx).enabled("local"))
    _attach_local_metadata(local)
    return 200, {"ok": True, "local": local}


def api_models_response(ctx: AppContext | None = None) -> tuple[int, dict[str, object]]:
    from codey.app.model_settings import _PayloadCallback, preferences_for
    from codey.providers.api_connections import connection_for

    preferences = preferences_for(ctx)
    payload: list[dict[str, object]] = []
    for connection_id in API_CONNECTIONS:
        if not preferences.enabled(connection_id):
            continue
        coordinator = ctx.connection_status if ctx is not None else None
        if coordinator is not None:
            coordinator.refresh(
                connection_id,
                force=not bool(ctx._provider_status_snapshot_requested if ctx is not None else False),
            )
            current = coordinator.current(connection_id)
            if connection_id == "local" and current.state == "checking":
                coordinator.wait_for(connection_id, timeout=2.0)
                current = coordinator.current(connection_id)
            cached_data: dict[str, object] = coordinator.data(connection_id)
            cached = cached_data.get("payload")
            if object_fields(cached):
                item = cached
                preferences.observe_catalog(item)
                payload.append(item)
                continue
            if cached_data:
                item = {
                    "id": connection_id,
                    "label": API_CONNECTIONS[connection_id][0],
                    "models": cached_data.get("models", []),
                }
                payload.append(item)
                continue
            payload.append({"id": connection_id, "label": API_CONNECTIONS[connection_id][0], "models": [],
                            "state": current.state})
            continue
        try:
            model_payload: object = connection_for(connection_id).model_payload
            if not isinstance(model_payload, _PayloadCallback):
                raise TypeError("model_payload must be callable")
            item = _response_fields(model_payload())
            preferences.observe_catalog(item)
            payload.append(item)
        except (OSError, ValueError, RuntimeError) as exc:
            payload.append({"id": connection_id, "label": API_CONNECTIONS[connection_id][0], "models": [], "error": str(exc)[:240]})
    return 200, {"connections": payload}


def _attach_local_metadata(local: dict[str, object]) -> None:
    from codey.providers.local_selection import model_metadata

    config = load_local_config()
    selection = select_local_target(config)
    local["saved_display_name"] = config.display_name
    # An empty base computes the same title without probing offline metadata.
    local.update(model_metadata(
        str(local.get("base_url") or "") if local.get("connected") else "",
        str(local.get("model") or ""),
        api_key=selection.api_key,
        display_name=config.display_name if selection.model == config.model and selection.base_url == config.base_url else "",
    ))
    if config.native_tools_mode == "json":
        local["thinking_options"] = ["off"]


def save_local_provider_response(body: object) -> tuple[int, dict[str, object]]:
    if not object_fields(body):
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
        thinking_enabled=parsed.thinking_enabled,
        display_name=parsed.display_name,
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
            thinking_enabled=parsed.thinking_enabled,
            display_name=parsed.display_name,
            api_protocol=parsed.api_protocol,
            connection_revision=parsed.connection_revision,
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
    raw_local: object = assemble_bootstrap_payload(saved, saved_selection, ordered, [])
    local = _response_fields(raw_local)
    _attach_local_metadata(local)
    return 200, {"ok": True, "local": local}


def research_unconfigured_response() -> tuple[int, dict[str, object]]:
    return 404, {"ok": False, "error": "Research is not configured"}


def research_graph_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    if ctx.knowledge_store is None:
        return research_unconfigured_response()
    # Lazy import: importing api must not load the knowledge graph stack.
    from codey.knowledge.concepts import build_unified_research_graph

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
    graph_payload: object = graph.to_dict()
    return 200, {"ok": True, "graph": _response_fields(graph_payload)}


def research_notes_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
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
    notes: dict[str, dict[str, object]] = {}
    missing: list[str] = []
    for note_id, (note, row) in ctx.knowledge_store.read_notes_with_rows(seen).items():
        notes[note_id] = _research_note_payload(note, row)
    for note_id in seen:
        if note_id not in notes:
            missing.append(note_id)
    return 200, {"ok": True, "notes": notes, "missing": missing}


def _research_note_payload(note: KnowledgeNote, row: NoteDocumentMetadata) -> dict[str, object]:
    return {
        "id": note.id,
        "type": note.type,
        "title": note.title,
        "body": note.body,
        "sources": note.sources,
        "tags": note.tags,
        "status": note.status,
        "path": row["path"],
        "updated": note.updated,
    }


def run_details_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    session_id = ((query.get("session_id") or [""])[0] or "").strip()
    run_id = ((query.get("run_id") or [""])[0] or "").strip()
    if not session_id or not run_id:
        return 400, {"ok": False, "error": "session_id and run_id required"}
    summary = load_run_details(
        run_ledgers=ctx.run_ledgers,
        run_traces=ctx.run_traces,
        runtime_operations=ctx.runtime_operations,
        runtime_effects=ctx.runtime_effects,
        tool_result_delivery=ctx.tool_result_delivery,
        session_id=session_id,
        run_id=run_id,
    )
    return 200, {
        "ok": True,
        "available": summary.available,
        "details": summary.to_jsonable(),
    }


def run_review_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    """Structured cold read using the same authenticated run lookup as Details."""
    from codey.reviews.core import review_result_payload
    from codey.reviews.persistence import load_recorded_review
    from codey.reviews.reuse import validate_source_run_id

    session_id = query_value(query, "session_id")
    run_id = query_value(query, "run_id")
    if not session_id or not run_id:
        return 400, {"ok": False, "error": "session_id and run_id required"}
    try:
        validate_source_run_id(run_id)
    except ValueError:
        return 400, {"ok": False, "error": "invalid run_id"}
    try:
        recorded: object = load_recorded_review(ctx.state_home, session_id, run_id)
    except (OSError, ValueError):
        recorded = None
    if recorded is None:
        return 200, {"ok": True, "available": False, "review": None}
    if not sequence_items(recorded) or len(recorded) != 2:
        raise TypeError("recorded review must contain a projection and result")
    result = recorded[1]
    if not _is_review_result(result):
        raise TypeError("recorded review result is invalid")
    raw_payload: object = review_result_payload(result)
    payload = _response_fields(raw_payload)
    payload["findings"] = [_dataclass_fields(finding) for finding in result.findings]
    scope: object = result.scope
    payload["scope"] = _dataclass_fields(scope)
    payload["summary"] = result.summary
    return 200, {"ok": True, "available": True, "review": payload}


def research_restore_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        return 400, {"ok": False, "error": "run_id required"}
    payload = ctx.restore_research_changes(run_id)
    return 200 if payload.get("ok") else 409, payload




def ghost_summary_response(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, object]]:
    return memory_summary(ctx, query)


def ghost_export_response(ctx: AppContext) -> tuple[int, dict[str, object]]:
    return 200, dict[str, object](ctx.memory.export_state())


def ghost_action_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    return memory_action(ctx, body)


def changes_response(ctx: AppContext, project: object) -> tuple[int, dict[str, object]]:
    project_text = str(project or "").strip()
    if "\0" in project_text:
        return 400, {"ok": False, "error": "project invalid", "files": [], "diff": ""}
    try:
        key = str(Path(project_text).expanduser().resolve()) if project_text else ""
    except (ValueError, OSError):
        return 400, {"ok": False, "error": "project invalid", "files": [], "diff": ""}
    try:
        tracker = (
            ctx.change_tracker_for(key, persistent=not is_git_repository(key))
            if key
            else None
        )
        changes: object = collect_changes(project_text, tracker)
        payload: dict[str, object] = dict(_response_fields(changes))
    except StoreCorruption:
        payload = {
            "ok": False,
            "error": "snapshot needs repair",
            "files": list[dict[str, object]](),
            "diff": "",
        }
    return 200 if payload.get("ok") else 400, payload


def changes_file_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    project = str((body.get("project") if isinstance(body, dict) else "") or "").strip()
    path = str((body.get("path") if isinstance(body, dict) else "") or "").strip()
    if not project or not path:
        return 400, {"ok": False, "error": "project and path required", "path": path}
    if "\0" in project or "\0" in path:
        return 400, {"ok": False, "error": "project or path invalid", "path": path}
    try:
        diff: object = collect_git_file_diff(project, path)
        payload = _response_fields(diff)
    except (OSError, ValueError):
        return 400, {"ok": False, "error": "file path invalid", "path": path}
    return 200 if payload.get("ok") else 400, payload


def restore_changes_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    project = str((body.get("project") if isinstance(body, dict) else "") or "").strip()
    if not project:
        return 400, {"ok": False, "error": "project required"}
    if "\0" in project:
        return 400, {"ok": False, "error": "project invalid"}
    paths = body.get("paths")
    if paths is not None and not isinstance(paths, list):
        return 400, {"ok": False, "error": "paths must be a list"}
    submitted_paths: list[object] | None = paths
    clean_paths = [str(path) for path in submitted_paths] if submitted_paths is not None else None
    preview = body.get("preview", False)
    if not isinstance(preview, bool):
        return 400, {"ok": False, "error": "preview must be a boolean"}
    try:
        key = str(Path(project).expanduser().resolve())
    except (ValueError, OSError):
        return 400, {"ok": False, "error": "project invalid"}
    if ctx.has_active_run_for_project(key):
        return 409, {"ok": False, "error": "run in progress"}
    # Single-writer promise covers tasks and user restore: hold the same
    # project writer lease from reading the recovery basis to finishing
    # file writes. Cross-process contention fails fast with 409.
    acquired = bool(ctx.acquire_project_writer(key))
    try:
        if not acquired:
            return 409, {"ok": False, "error": "project writer busy"}
        tracker: ChangeTracker | None = ctx.change_tracker_for(
            key,
            persistent=not is_git_repository(key),
        )
        if tracker is not None and not tracker.has_snapshots:
            tracker = None
        response: object = restore_snapshot_changes(project, tracker, clean_paths, preview=preview)
        return _response_pair(response)
    finally:
        if acquired:
            with contextlib.suppress(Exception):
                ctx.release_project_writer(key)


def _model_disabled(ctx: AppContext | None, body: dict[str, object], provider_id: str) -> bool:
    from codey.app.model_settings import preferences_for

    selection = body.get("model_selection")
    model_id = str(selection.get("model") or "") if object_fields(selection) else ""
    return not preferences_for(ctx).allows(provider_id, model_id)


def _api_selection_for_submit(ctx: AppContext | None, provider_id: str, selection: object) -> tuple[TaskSubmitOptions, tuple[int, dict[str, object]] | None]:
    if provider_id not in API_CONNECTIONS:
        return {}, (400, {"error": "model_selection requires an API model"})
    from codey.app.model_settings import preferences_for
    from codey.providers.api_connections import capture_selection

    try:
        if not object_fields(selection):
            raise ValueError("Choose an explicit model")
        model = selection.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Choose an explicit model")
        captured = capture_selection(provider_id, selection)
        if not preferences_for(ctx).allows(provider_id, captured.model_id):
            return {}, (409, {"code": "model_disabled", "error": "Selected model is disabled. Choose an enabled model in Settings."})
        return {"model_selection": captured}, None
    except ValueError as exc:
        return {}, (400, {"error": str(exc), "reason": "model_selection_invalid"})


def _approval_entry(ctx: AppContext | None, session_id: str, source_run_id: str) -> tuple[str, tuple[int, dict[str, object]] | None]:
    if ctx is None:
        return source_run_id, None
    state = ctx.shell_approval_workflow.for_source(session_id, source_run_id)
    if state is None or state['decision'] == 'cancelled':
        return source_run_id, None
    if state['continuation'] == 'started':
        return (str(state['continuation_run_id']) if source_run_id else ''), None
    code = 'approval_required' if state['decision'] == 'pending' else 'approval_in_progress'
    if state['execution'] == 'uncertain':
        code = 'execution_uncertain'
    elif state['continuation'] in {'queued', 'failed', 'resuming'}:
        code = 'approval_resume_required'
    elif state['continuation'] in {'not_required', 'cancelled'}:
        return source_run_id, None
    return source_run_id, (409, {'error': {
        'approval_required': 'Decide the pending command approval first.',
        'execution_uncertain': 'Command outcome is uncertain; automatic execution is blocked.',
        'approval_resume_required': 'Continue from the saved command result.',
        'approval_in_progress': 'Command approval is being processed.',
    }[code], 'code': code, 'approval': state})


def _completion_fields(body: dict[str, object]) -> TaskSubmitOptions:
    from codey.policies.tool_requirement import required_tool_names

    output = submission_fields(body)
    fields: TaskSubmitOptions = {}
    if output:
        fields.update({"output_format": output["output_format"], "output_schema": output["output_schema"]})
    names = required_tool_names(body.get("required_tools", ()))
    if names:
        fields["required_tools"] = names
    return fields


def _submission_input(value: object) -> StartTask | EditUserMessage | ResumeRun | HandoffRun | None:
    if value is None:
        return None
    if not object_fields(value):
        raise ValueError("input must be an object")
    kind = str(value.get("kind") or "").strip().lower()
    if kind in {"start_task", "edit_user_message"}:
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input.text required")
        if kind == "start_task":
            return StartTask(text.strip())
        entry_id, revision = value.get("entry_id"), value.get("expected_revision")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("input.entry_id required")
        if type(revision) is not int or revision < 0:
            raise ValueError("input.expected_revision must be a non-negative integer")
        return EditUserMessage(entry_id, text.strip(), revision)
    if kind not in {"resume_run", "handoff_run"}:
        raise ValueError("unsupported input.kind")
    source = value.get("source_run_id")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("input.source_run_id required")
    instruction = value.get("instruction", "")
    if kind == "resume_run":
        return ResumeRun(source.strip(), str(instruction).strip())
    observe = value.get("observe_only", False)
    if not isinstance(instruction, str) or not instruction.strip() or not isinstance(observe, bool):
        raise ValueError("handoff requires an instruction and a boolean observe_only")
    return HandoffRun(source.strip(), instruction.strip(), observe)


def run_submit_response(  # noqa: C901, PLR0912
    body: object,
    submit_task: TaskSubmitter,
    *,
    ctx: AppContext | None = None,
    captured_selection: ApiRunSelection | None = None,
    captured_auth: EntryAuth | None = None,
) -> tuple[int, dict[str, object]]:  # noqa: C901, PLR0912
    if not object_fields(body):
        return 400, {"error": "invalid json"}
    if "continue_task" in body:
        return 400, {"error": "continue_task is no longer accepted; use input.kind=resume_run"}
    try:
        input = _submission_input(body.get("input"))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    input_source_run = input.source_run_id if isinstance(input, ResumeRun) else ""
    session_id = str(body.get("session_id") or "").strip() or "default"
    question = _pending_question(ctx, session_id)
    if question is not None:
        return 409, {"error": "Answer the pending question before starting another task.", "code": "question_pending", "question": question}
    input_source_run, approval_error = _approval_entry(ctx, session_id, input_source_run)
    if approval_error is not None:
        return approval_error
    if ctx is not None and ctx.shell_approval_workflow.busy_status(session_id) is not None:
        return 409, {"error": "busy", "hint": "approval_in_progress"}
    client_run_id = body.get("run_id", "")
    if not isinstance(client_run_id, str) or (client_run_id and not re.fullmatch(r"run_[a-f0-9]{32}", client_run_id)):
        return 400, {"error": "invalid run_id"}
    project = str(body.get("project") or "").strip() or None
    task = input.text if isinstance(input, (StartTask, EditUserMessage)) else (
        input.instruction if isinstance(input, (ResumeRun, HandoffRun)) else str(body.get("task") or "").strip())
    continue_task = isinstance(input, ResumeRun)
    provider_id = str(body.get("provider") or DEFAULT_PROVIDER_ID).strip().lower()
    intent = str(body.get("intent") or "auto").strip().lower()
    entry_auth = captured_auth or derive_entry_auth({**body, "task": task}, project=project)
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
    raw_max_turns = body.get("max_turns")
    if isinstance(raw_max_turns, bool) or (raw_max_turns is not None and not isinstance(raw_max_turns, (str, int, float))):
        return 400, {"error": "invalid max_turns"}
    try:
        max_turns = int(raw_max_turns or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError, OverflowError):
        return 400, {"error": "invalid max_turns"}
    max_turns = max(1, min(max_turns, 500))
    if not task and continue_task:
        task = "Continue the source run"
    if not task:
        return 400, {"error": "task required"}
    if provider_id not in PROVIDER_LABELS:
        return 400, {"error": f"unsupported provider: {provider_id}"}
    if captured_selection is not None and captured_selection.connection_id != provider_id:
        return 400, {"error": "captured model connection changed"}
    if captured_selection is not None:
        from codey.app.model_settings import preferences_for

        disabled = not preferences_for(ctx).allows(provider_id, captured_selection.model_id)
    else:
        disabled = _model_disabled(ctx, body, provider_id)
    if disabled:
        return 409, {"error": "Selected model is disabled. Choose an enabled model in Settings.", "code": "model_disabled"}
    if intent == "review" and not project:
        return 400, {"error": "project required for review"}
    from codey.reviews.reuse import validate_source_run_id

    try:
        review_source_run_id = validate_source_run_id(body.get("review_source_run_id", ""))
        output_fields = _completion_fields(body)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if review_source_run_id and intent not in {"review", "project"}:
        return 400, {"error": "review_source_run_id requires review or project intent"}
    project_error = _project_directory_error(project)
    if project_error:
        return 400, {"error": project_error}
    submit_kwargs: TaskSubmitOptions = {
        "requested_capabilities": entry_auth.requested_capabilities,
        "strict_research": entry_auth.strict_research,
        "sources_open_required": entry_auth.sources_open_required,
        "project_changes_required": entry_auth.project_changes_required,
        "denied_capabilities": entry_auth.denied_capabilities,
    }
    if review_source_run_id:
        submit_kwargs["review_source_run_id"] = review_source_run_id
    submit_kwargs.update(output_fields)
    if isinstance(input, HandoffRun):
        if provider_id not in API_CONNECTIONS or not isinstance(body.get("model_selection"), dict) or intent == "review":
            return 400, {"error": "handoff requires an explicit API model selection for a model task"}
        if input.observe_only and entry_auth.project_changes_required:
            return 400, {"error": "observation-only handoff cannot require project writes"}
    if isinstance(input, (EditUserMessage, HandoffRun)):
        submit_kwargs["input"] = input
    if client_run_id:
        submit_kwargs["run_id"] = client_run_id
    if input_source_run:
        submit_kwargs["previous_run_id"] = input_source_run
    if captured_selection is not None:
        submit_kwargs["model_selection"] = captured_selection
    elif provider_id in API_CONNECTIONS or "model_selection" in body:
        captured, error = _api_selection_for_submit(ctx, provider_id, body.get("model_selection"))
        if error:
            return error
        submit_kwargs.update(captured)
    return _submit_with_attachments(ctx, body, submit_task,
        (session_id, project, task, max_turns, continue_task, provider_id,
         (entry_auth.inferred_intent or intent) if intent == "auto" and not continue_task else intent), submit_kwargs)


def _submit_with_attachments(ctx: AppContext | None, body: dict[str, object], submit: TaskSubmitter, args: _SubmissionArgs, kwargs: TaskSubmitOptions) -> tuple[int, dict[str, object]]:
    if "attachments" in body:
        from codey.app.attachment_routes import prepare_run_attachments

        error = prepare_run_attachments(ctx, str(args[0]), body["attachments"], kwargs)
        if error:
            return error
    return _submit_task_response(submit, args, kwargs)


def _submit_task_response(submit_task: TaskSubmitter, args: _SubmissionArgs, kwargs: TaskSubmitOptions) -> tuple[int, dict[str, object]]:
    try:
        run_id = submit_task(*args, **kwargs)
    except BrowserWorkerBusy:
        return 503, {"error": "browser worker busy", "hint": "retry"}
    except ModelDisabledError as exc:
        return 409, {"error": str(exc), "code": "model_disabled"}
    except ModelHandoffRejected as exc:
        return 409, {"error": str(exc), "code": "model_handoff_rejected"}
    except (CommitConflict, IdempotencyConflict) as exc:
        return 409, {"error": str(exc), "code": "conversation_conflict"}
    except Exception as exc:
        return 500, {"error": str(exc)}
    if run_id is None:
        return 409, {"error": "busy", "hint": "try_continue"}
    return 200, {"ok": True, "run_id": run_id}


def shell_approval_response(
    ctx: AppContext,
    body: dict[str, object],
    *,
    submit_task_after_slot_release: ContinuationSubmitter,
) -> tuple[int, dict[str, object]]:
    approval_id = str(body.get("id") or "").strip()
    approved = body.get("approved")
    if type(approved) is not bool:
        return 400, {"error": "approved must be a boolean"}
    response: tuple[int, dict[str, object]] = ctx.shell_approval_workflow.accept(
        approval_id, approved, submit_task_after_slot_release, scope=str(body.get("scope", "once")),
    )
    return response


def teach_resume_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    teach_id = str(body.get("id") or "").strip()
    if not ctx.resume_pending_teach(teach_id):
        return 404, {"error": "pause not found"}
    return 200, {"ok": True}


def new_chat_response(ctx: AppContext, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return 400, {"ok": False, "error": "session_id required"}
    if ctx.active_run_for(session_id=session_id) is not None:
        return 409, {"ok": False, "error": "run in progress"}
    try:
        failures = ctx.forget_conversation(session_id, preserve_ui=body.get("preserve_ui", True) is not False)
    except CommitConflict as exc:
        return 409, {"ok": False, "error": str(exc)}
    if failures:
        return 503, {
            "ok": False,
            "error": "Conversation cleanup incomplete; retry deletion.",
            "state": ctx.load_ui_state(),
            "warnings": [f"Failed to purge store {k}: {v}" for k, v in failures.items()],
            "unpurged_stores": list(failures.keys()),
        }
    return 200, {"ok": True, "state": ctx.load_ui_state()}


def stop_response(ctx: AppContext, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    run_id = str((body or {}).get("run_id") or "").strip() or None
    ctx.request_stop(run_id)
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
    "run_review_response",
    "run_submit_response",
    "save_local_provider_response",
    "save_ui_state_response",
    "shell_approval_response",
    "stop_response",
    "teach_resume_response",
    "ui_state_response",
]
