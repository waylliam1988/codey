"""Tiny HTTP + SSE server that drives the agent from a native UI.

Requires pywebview in addition to the standard library plus Playwright
(already used).

Endpoints
    GET  /                serves codey/web/index.html
    GET  /api/state       returns current run state as JSON
    GET  /api/ui_state    returns durable sidebar/chat UI state
    POST /api/ui_state    stores durable sidebar/chat UI state
    POST /api/run         body {project, task, provider, max_turns} → starts agent in
                          a background thread, returns {ok:true, run_id}
    POST /api/changes     body {project} → returns git status + diff
    GET  /api/ghost/summary query {session_id, project} → returns bounded
                          local context summary
    POST /api/ghost/action body {action, ...} → reviews or deletes local
                          context state without provider/tool execution
    GET  /api/run_details query {session_id, run_id} → returns a bounded,
                          user-facing run explanation
    POST /api/shell_approval body {id, approved} → approve/reject shell request
    POST /api/stop        request cooperative stop of the current task
    GET  /api/events      Server-Sent Events stream of log lines

A single Codey instance can run one task at a time; while a task is running
new /api/run calls return 409.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from codey import __version__
from codey.app import api as app_api
from codey.app import model_settings
from codey.app import provider_services as provider_services
from codey.app import task_submit as task_submit
from codey.app.context import AppContext
from codey.app.conversation_routes import conversation_action_response, conversation_tree_response
from codey.app.documents import document_response
from codey.app.durable_events import replay_commits
from codey.app.event_payloads import browser_event_payload
from codey.app.http_plumbing import (
    WEB_DIR,
    request_origin_allowed,
    resolve_web_asset,
    send_file,
    send_index,
    send_json,
    sse_cursor,
    validate_web_root,
    write_sse_event,
)
from codey.app.input_queue_routes import bind_input_queue, input_action_response, inputs_response
from codey.app.operator_auth import OperatorAuth
from codey.app.question_routes import question_answer_response, questions_response
from codey.app.workflow_routes import workflow_fork_response, workflow_history_response
from codey.runtime.core.api_selection import ApiRunSelection
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.task.model import TaskInput

FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_CONTINUATION_IDLE_TIMEOUT = task_submit.SHELL_CONTINUATION_IDLE_TIMEOUT
MAX_POST_BODY_BYTES = 16 * 1024 * 1024
POST_BODY_READ_TIMEOUT = 10.0


def _event_commit_seq(event: object) -> int:
    if not isinstance(event, dict):
        return 0
    value = event.get("commit_seq")
    return value if type(value) is int and value > 0 else 0


def _is_replayed_durable_commit(event: object, durable_cursor: int) -> bool:
    """Drop the EventBus copy after the durable commit was replayed.

    Durable commits are reconstructed from SQLite first because the in-memory
    EventBus is lost on restart.  A still-retained EventBus replay can contain
    the same ``durable_commit`` projection, so sending both copies would make
    the browser apply one committed fact twice.  Other runtime events remain
    eligible for EventBus replay.
    """
    if not isinstance(event, dict) or event.get("type") != "durable_commit":
        return False
    commit_seq = _event_commit_seq(event)
    return commit_seq > 0 and commit_seq <= max(0, int(durable_cursor))


class CodeyHTTPServer(ThreadingHTTPServer):
    """Keep routine browser disconnects out of the local server log."""

    request_queue_size = 32

    def __init__(self, server_address: tuple[str, int], request_handler_class: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(server_address, request_handler_class)
        validate_web_root()
        self.operator_auth = OperatorAuth(self.server_address[1])

    def launch_url(self, base_url: str) -> str:
        return f"{base_url}#codey_bootstrap={self.operator_auth.issue_bootstrap()}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


STATE: AppContext | None = None
_STATE_LOCK = threading.Lock()


def _build_state() -> AppContext:
    from codey.app import sibling_probe

    state = AppContext(DEFAULT_STATE_HOME)
    sibling_probe.bind_provider_handlers(state)
    return state


def get_state() -> AppContext:
    """Lazy global state: importing this module must not construct stores.

    A test-patched ``server.STATE`` (not None) is honored as-is so existing
    ``mock.patch.object(server, "STATE", state)`` doubles keep working."""
    current = STATE
    if current is not None:
        return current
    with _STATE_LOCK:
        current = STATE
        if current is not None:
            return current
        built = _build_state()
        globals()["STATE"] = built
        built.shell_approval_workflow.start(_submit_task_after_slot_release)
        return built


def pick_folder(mode: str = "open", initial: str | None = None) -> str | None:
    """Open a native folder picker and return the selected absolute path.

    Browsers cannot expose an arbitrary local folder path to JavaScript, so the
    local server owns this action.  Tkinter ships with Python and gives us the
    standard Windows folder dialog without adding dependencies.
    """
    import tkinter as tk
    from tkinter import filedialog

    title = "Select Existing Project Folder"
    mustexist = True
    if mode == "new":
        title = "Create or Select Project Folder"
        mustexist = False

    initial_path = Path(initial).expanduser() if initial else Path.home()
    if not initial_path.exists():
        initial_path = Path.home()
    initialdir = str(initial_path)
    with FOLDER_DIALOG_LOCK:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=root,
                title=title,
                initialdir=initialdir,
                mustexist=mustexist,
            )
        finally:
            root.destroy()

    if not selected:
        return None
    path = Path(selected).resolve()
    if mode == "new":
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ----------------------------------------------------------- task runner ---
# Implementation lives in task_submit.py; these bind the HTTP-layer get_state
# so existing mock.patch.object(server, "_submit_task") seams keep working.

def _submit_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    *,
    requested_capabilities: tuple[str, ...] = (),
    strict_research: bool = False,
    sources_open_required: bool = False,
    project_changes_required: bool = False,
    denied_capabilities: tuple[str, ...] = (),
    previous_run_id: str = "",
    initial_shell_results: tuple[dict[str, object], ...] = (),
    review_source_run_id: str = "",
    model_selection: ApiRunSelection | None = None,
    run_id: str = "",
    input: TaskInput | None = None,
    abort_if_stopped: bool = False,
) -> str | None:
    return task_submit.submit_task(
        session_id,
        project,
        task,
        max_turns,
        continue_task,
        provider_id,
        intent,
        get_state=get_state,
        abort_if_stopped=abort_if_stopped,
        requested_capabilities=requested_capabilities,
        strict_research=strict_research,
        sources_open_required=sources_open_required,
        project_changes_required=project_changes_required,
        denied_capabilities=denied_capabilities,
        previous_run_id=previous_run_id,
        initial_shell_results=initial_shell_results,
        review_source_run_id=review_source_run_id,
        model_selection=model_selection,
        run_id=run_id,
        input=input,
    )


def _submit_task_after_slot_release(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    *,
    previous_run_id: str = "",
    timeout: float = SHELL_CONTINUATION_IDLE_TIMEOUT,
    initial_shell_results: tuple[dict[str, object], ...] = (),
    model_selection: ApiRunSelection | None = None,
    input: TaskInput | None = None,
    run_id: str = "",
) -> str | None:
    return task_submit.submit_task_after_slot_release(
        session_id,
        project,
        task,
        max_turns,
        continue_task,
        provider_id,
        intent,
        get_state=get_state,
        previous_run_id=previous_run_id,
        timeout=timeout,
        initial_shell_results=initial_shell_results,
        model_selection=model_selection,
        input=input,
        run_id=run_id,
    )


def _pick_folder_response(_ctx: AppContext, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    mode = str(body.get("mode") or "open").strip().lower()
    if mode not in {"open", "new"}:
        return 400, {"error": "invalid mode"}
    initial = str(body.get("initial") or "").strip() or None
    try:
        path = pick_folder(mode=mode, initial=initial)
    except Exception as exc:
        return 500, {"error": str(exc)}
    if not path:
        return 200, {"ok": False, "cancelled": True}
    return 200, {"ok": True, "path": path, "name": Path(path).name or path}


def _run_submit_route(_ctx: AppContext, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response: tuple[int, dict[str, Any]] = app_api.run_submit_response(body, _submit_task, ctx=_ctx)
    return response


def _shell_approval_status_route(ctx: AppContext, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    approval_id = str((query.get("id") or [""])[0])
    if approval_id:
        state = ctx.shell_approval_workflow.status(approval_id)
        return (200, dict(state)) if state is not None else (404, {"error": "approval not found"})
    return 200, {"approvals": ctx.shell_approval_workflow.list_statuses(
        session_id=str((query.get("session_id") or [""])[0]),
    )}


def _shell_approval_route(ctx: AppContext, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response: tuple[int, dict[str, Any]] = app_api.shell_approval_response(
        ctx,
        body,
        submit_task_after_slot_release=_submit_task_after_slot_release,
    )
    return response


def _transcript_route(ctx: AppContext, session_id: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    # A newly created chat has UI metadata before it has a durable conversation.
    # Its first history read is therefore a valid empty snapshot, rather than a
    # load failure.  The conversation is created when the first message is
    # admitted (or when an explicit durable operation needs it).
    if not ctx.durable.has_conversation(session_id):
        return 200, {
            "schema_version": 1,
            "session_id": session_id,
            "epoch": ctx.durable.database_id,
            "snapshot_revision": 0,
            "items": [],
            "before_cursor": None,
            "has_more": False,
        }
    try:
        before = str((query.get("before") or [""])[0]) or None
        limit = int((query.get("limit") or ["100"])[0])
        return 200, ctx.durable.history_page(session_id, before=before, limit=limit)
    except (KeyError, ValueError, TypeError) as exc:
        return 400, {"ok": False, "error": str(exc)}


def _transcript_search_route(ctx: AppContext, session_id: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    value = str((query.get("q") or [""])[0])
    if len(value) > 200:
        return 400, {"ok": False, "error": "query too long"}
    try:
        hits = ctx.durable.search_history(session_id, value)
    except KeyError as exc:
        return 404, {"ok": False, "error": str(exc)}
    return 200, {"schema_version": 1, "session_id": session_id, "hits": hits}


def _execution_output_route(ctx: AppContext, execution_id: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
    try:
        snapshot = ctx.execution_outputs.snapshot(execution_id)
    except KeyError:
        return 404, {"ok": False, "error": "execution_not_found"}
    requested_session = str((query.get("session_id") or [""])[0] or "")
    requested_run = str((query.get("run_id") or [""])[0] or "")
    if not requested_session or not requested_run:
        return 403, {"ok": False, "error": "execution_scope_required"}
    if (requested_session and requested_session != snapshot["session_id"]) or (requested_run and requested_run != snapshot["run_id"]):
        return 404, {"ok": False, "error": "execution_not_found"}
    after = (query.get("after_revision") or [""])[0]
    if after and after.isdigit() and int(after) >= int(snapshot["revision"]):
        return 304, snapshot
    return 200, snapshot


_GET_ROUTES: dict[str, Callable[[AppContext, dict[str, list[str]]], tuple[int, dict[str, Any]]]] = {
    "/api/conversation_tree": conversation_tree_response,
    "/api/workflows": workflow_history_response,
    "/api/inputs": inputs_response,
    "/api/questions": questions_response,
    "/api/model_settings": lambda ctx, _query: model_settings.settings_response(ctx),
    "/api/state": lambda ctx, _query: (200, ctx.run_state_payload()),
    "/api/shell_approval": lambda ctx, query: _shell_approval_status_route(ctx, query),
    "/api/ui_state": lambda ctx, _query: app_api.ui_state_response(ctx),
    "/api/providers": lambda ctx, _query: app_api.providers_response(ctx),
    "/api/provider_catalog": lambda _ctx, _query: app_api.provider_catalog_response(),
    "/api/local_provider": lambda ctx, _query: app_api.local_provider_response(ctx),
    "/api/api_models": lambda ctx, _query: app_api.api_models_response(ctx),
    "/api/research/graph": app_api.research_graph_response,
    "/api/run_details": app_api.run_details_response,
    "/api/run_review": app_api.run_review_response,
    "/api/ghost/summary": app_api.ghost_summary_response,
    "/api/ghost/export": lambda ctx, _query: app_api.ghost_export_response(ctx),
}


_POST_ROUTES: dict[str, Callable[[AppContext, dict[str, Any]], tuple[int, dict[str, Any]]]] = {
    "/api/conversation_tree/action": conversation_action_response,
    "/api/workflows/fork": workflow_fork_response,
    "/api/inputs": input_action_response,
    "/api/questions": question_answer_response,
    "/api/model_settings": model_settings.save_response,
    "/api/model_catalog": model_settings.discover_response,
    "/api/ui_state": app_api.save_ui_state_response,
    "/api/local_provider": lambda _ctx, body: app_api.save_local_provider_response(body),
    "/api/run": _run_submit_route,
    "/api/research/restore": app_api.research_restore_response,
    "/api/research/notes": app_api.research_notes_response,
    "/api/ghost/action": app_api.ghost_action_response,
    "/api/pick_folder": _pick_folder_response,
    "/api/changes": lambda ctx, body: app_api.changes_response(
        ctx,
        str((body.get("project") if isinstance(body, dict) else "") or ""),
    ),
    "/api/changes/file": app_api.changes_file_response,
    "/api/documents/read": document_response,
    "/api/changes/restore": app_api.restore_changes_response,
    "/api/shell_approval": _shell_approval_route,
    "/api/shell_approval/retry": lambda ctx, body: ctx.shell_approval_workflow.retry(
        str(body.get("id") or ""), _submit_task_after_slot_release,
    ),
    "/api/teach/resume": app_api.teach_resume_response,
    "/api/new_chat": app_api.new_chat_response,
    "/api/stop": lambda ctx, body: app_api.stop_response(ctx, body),
}


# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"Codey/{__version__}"

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet the default access log.
        pass

    def _request_origin_allowed(self) -> bool:
        allowed: bool = request_origin_allowed(self)
        return allowed

    def _deny_foreign_origin(self) -> None:
        self._send_json(403, {"error": "cross-origin request refused"})

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        send_json(self, status, payload)

    def _send_file(self, path: Path, ctype: str, *, immutable: bool = False) -> None:
        send_file(self, path, ctype, immutable=immutable)

    def _send_index(self) -> None:
        send_index(self)

    def _require_operator(self) -> bool:
        auth = cast(CodeyHTTPServer, self.server).operator_auth
        if auth.authenticated(self.headers.get("Cookie")):
            return True
        self._send_json(401, {"error": "operator authentication required"})
        return False

    def _exchange_operator(self, body: object) -> None:
        auth = cast(CodeyHTTPServer, self.server).operator_auth
        cookie = auth.exchange(body.get("token")) if isinstance(body, dict) and set(body) == {"token"} else None
        if cookie is None:
            self._send_json(401, {"error": "operator authentication required"})
            return
        data = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_post_body(self, length: int) -> bytes | None:
        """Bounded body read: slow clients get 408 instead of a thread."""
        if not length:
            return b""
        connection = getattr(self, "connection", None)
        set_timeout = getattr(connection, "settimeout", None)
        previous_timeout: object = None
        if connection is not None and callable(set_timeout):
            try:
                previous_timeout = connection.gettimeout()
            except Exception:
                previous_timeout = None
            try:
                connection.settimeout(POST_BODY_READ_TIMEOUT)
            except Exception:
                set_timeout = None
        try:
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    self._send_json(400, {"error": "incomplete request body"})
                    return None
                return body
            except TimeoutError:
                self._send_json(408, {"error": "request body timeout"})
                return None
            except Exception:
                self._send_json(400, {"error": "request body unreadable"})
                return None
        finally:
            if connection is not None and callable(set_timeout):
                with contextlib.suppress(Exception):
                    connection.settimeout(previous_timeout)

    def do_GET(self) -> None:  # noqa: N802, C901, PLR0912
        if not self._request_origin_allowed():
            self._deny_foreign_origin()
            return
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send_index()
            return
        if url.path.startswith("/assets/"):
            asset = resolve_web_asset(url.path)
            if asset is None:
                self.send_response(404)
                self.end_headers()
            else:
                pinned = "v" in parse_qs(url.query)
                self._send_file(asset[0], asset[1], immutable=pinned)
            return
        if url.path == "/icon.ico":
            icon = WEB_DIR / "icon.ico"
            if icon.is_file():
                self._send_file(icon, "image/x-icon")
            else:
                self.send_response(404)
                self.end_headers()
            return
        if not self._require_operator():
            return
        if url.path == "/api/operator_session":
            self._send_json(200, {"ok": True})
            return
        if url.path.startswith("/api/sessions/") and url.path.endswith("/messages/search"):
            parts = url.path.split("/")
            if len(parts) == 6 and parts[3]:
                try:
                    status, payload = _transcript_search_route(get_state(), parts[3], parse_qs(url.query))
                except Exception as exc:
                    self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return
                self._send_json(status, payload)
                return
        if url.path.startswith("/api/sessions/") and url.path.endswith("/messages"):
            parts = url.path.split("/")
            if len(parts) == 5 and parts[3]:
                try:
                    status, payload = _transcript_route(get_state(), parts[3], parse_qs(url.query))
                except Exception as exc:
                    self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return
                self._send_json(status, payload)
                return
        if url.path.startswith("/api/executions/") and url.path.endswith("/output"):
            parts = url.path.split("/")
            if len(parts) == 5 and parts[3]:
                try:
                    status, payload = _execution_output_route(get_state(), parts[3], parse_qs(url.query))
                except Exception as exc:
                    self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                    return
                if status == 304:
                    self.send_response(304)
                    self.end_headers()
                else:
                    self._send_json(status, payload)
                return
        route = _GET_ROUTES.get(url.path)
        if route is not None:
            try:
                status, payload = route(get_state(), parse_qs(url.query))
            except Exception as exc:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(status, payload)
            return
        if url.path == "/api/events":
            self._sse()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if not self._request_origin_allowed():
            self._deny_foreign_origin()
            return
        url = urlparse(self.path)
        exchange = url.path == "/api/operator_session"
        if not exchange and not self._require_operator():
            return
        raw_length: object = self.headers.get("Content-Length", "0")
        if isinstance(raw_length, bool) or not isinstance(raw_length, str):
            self._send_json(400, {"error": "invalid content length"})
            return
        raw_length = raw_length.strip()
        if not raw_length.isascii() or not raw_length.isdigit():
            self._send_json(400, {"error": "invalid content length"})
            return
        try:
            length = int(raw_length)
        except (ValueError, OverflowError):
            self._send_json(400, {"error": "invalid content length"})
            return
        if length < 0:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length > (1024 if exchange else MAX_POST_BODY_BYTES):
            self._send_json(413, {"error": "request body too large"})
            return
        raw = self._read_post_body(length)
        if raw is None:
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else dict[str, object]()
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return

        if exchange:
            self._exchange_operator(body)
            return
        route = _POST_ROUTES.get(url.path)
        if route is not None:
            try:
                status, payload = route(get_state(), body)
            except Exception as exc:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(status, payload)
            return
        self.send_response(404)
        self.end_headers()

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        raw_header = self.headers.get("Last-Event-ID")
        if not raw_header:
            query = parse_qs(urlparse(self.path).query)
            raw_header = (query.get("last_event_id") or [""])[0]
        replay_cursor, header_commit_cursor = sse_cursor(raw_header)
        query = parse_qs(urlparse(self.path).query)
        raw_commit_cursor = (query.get("after_commit_seq") or [""])[0]
        try:
            commit_cursor = int(raw_commit_cursor) if raw_commit_cursor else header_commit_cursor
            commit_cursor = max(commit_cursor, header_commit_cursor)
        except ValueError:
            commit_cursor = 0
        state = get_state()
        q = state.subscribe()
        try:
            high_water = state.durable.commit_sequence()
            database_id = state.durable.database_id
            client_database_id = (query.get("database_id") or [""])[0]
            replay_requested = replay_cursor > 0 or commit_cursor > 0 or bool(client_database_id)
            if client_database_id != database_id or commit_cursor > high_water:
                commit_cursor = 0
            if not self._write_sse_event({
                "type": "hello", "status": state.run_status(),
                "database_id": database_id, "after_commit_seq": commit_cursor,
            }):
                return
            if replay_requested:
                for record in replay_commits(state.durable, commit_cursor):
                    commit_cursor = record.sequence
                    if not self._write_sse_event(
                        record.to_payload(),
                        event_id=f"{q.replay_cutoff}:{commit_cursor}",
                    ):
                        return
                if replay_cursor > 0:
                    for event_id, replay in state.replay_events_after(
                        replay_cursor,
                        max_event_id=q.replay_cutoff,
                    ):
                        public = browser_event_payload(replay) or replay
                        if _is_replayed_durable_commit(public, commit_cursor):
                            continue
                        commit_cursor = max(commit_cursor, _event_commit_seq(public))
                        if not self._write_sse_event(public, event_id=f"{event_id}:{commit_cursor}"):
                            return
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    continue
                public = browser_event_payload(ev) or ev
                if _is_replayed_durable_commit(public, commit_cursor):
                    continue
                commit_cursor = max(commit_cursor, _event_commit_seq(public))
                if not self._write_sse_event(
                    public,
                    event_id=f"{getattr(ev, 'event_id', 0)}:{commit_cursor}",
                ):
                    break
        finally:
            state.unsubscribe(q)

    def _write_sse_event(self, event: dict[str, Any], *, event_id: int | str = 0) -> bool:
        written: bool = write_sse_event(self, event, event_id=event_id)
        return written


def _wait_for_manual_browser(url: str, exc: Exception) -> None:
    print(f"[codey] Could not open native window: {exc}")
    print(f"[codey] Open this URL in your browser instead: {url}")
    if sys.platform == "win32":
        print("[codey] If needed, install Microsoft Edge WebView2 Runtime.")
    print("[codey] Press Ctrl+C to stop.")
    while True:
        time.sleep(3600)


def serve(host: str = "127.0.0.1", port: int = 5173) -> None:
    from codey.storage.file_lock import LockTimeout, acquire_lease

    try:
        server_lease = acquire_lease(DEFAULT_STATE_HOME / ".server.lock", timeout_seconds=0.0)
    except LockTimeout as exc:
        raise RuntimeError("该 state home 已在使用") from exc
    httpd: CodeyHTTPServer | None = None
    http_thread_started = False
    try:
        httpd = CodeyHTTPServer((host, port), Handler)
        actual_port = httpd.server_address[1]
        url = f"http://{host}:{actual_port}/"
        launch_url = httpd.launch_url(url)
        bind_input_queue(get_state())

        def _run_httpd() -> None:
            assert httpd is not None
            with contextlib.suppress(KeyboardInterrupt):
                httpd.serve_forever()

        threading.Thread(target=_run_httpd, daemon=True).start()
        http_thread_started = True
        print(f"[codey] UI ready: {url}")
    except BaseException:
        if httpd is not None:
            if http_thread_started:
                with contextlib.suppress(Exception):
                    httpd.shutdown()
            with contextlib.suppress(Exception):
                httpd.server_close()
        with contextlib.suppress(Exception):
            server_lease.release()
        raise

    def _run_webview() -> None:
        import webview

        icon = WEB_DIR / "icon.ico"
        webview.create_window("Codey", launch_url, width=1380, height=900, text_select=True)
        start_kwargs = {
            "private_mode": False,
            "storage_path": str(DEFAULT_STATE_HOME / "webview"),
        }
        if icon.is_file():
            start_kwargs["icon"] = str(icon)
        webview_start = cast(Callable[..., Any], webview.start)
        webview_start(**start_kwargs)

    try:
        _run_webview()
    except KeyboardInterrupt:
        print("\n[codey] shutting down")
    except Exception as exc:
        try:
            _wait_for_manual_browser(httpd.launch_url(url), exc)
        except KeyboardInterrupt:
            print("\n[codey] shutting down")
    finally:
        assert httpd is not None
        try:
            httpd.shutdown()
        finally:
            try:
                httpd.server_close()
            finally:
                with contextlib.suppress(Exception):
                    server_lease.release()
