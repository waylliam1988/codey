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

import json
import queue
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey.providers import controls as provider_controls, flow as provider_flow
from codey import __version__
from codey.agents.runner import run as agent_run
from codey.app import api as app_api
from codey.app import services as app_services
from codey.app.context import (
    REVIEW_FIX_TURNS,
    REVIEW_LOG_LINES,
    AppContext,
    _should_wait_for_local_ghost_sleep,
)
from codey.app.http_plumbing import (
    WEB_DIR,
    request_origin_allowed,
    resolve_web_asset,
    send_file,
    send_index,
    send_json,
    sse_replay_cursor,
    write_sse_event,
)
from codey.automation.browser_worker import BrowserWorkerBusy
from codey.automation.browser_worker import submit as submit_browser_task
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.providers import connect_fresh_provider_tab
from codey.workspace.changes import collect_changes, is_git_repository
from codey.providers.diagnostics import capture_provider_failure
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission

FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_CONTINUATION_IDLE_TIMEOUT = 15.0
MAX_POST_BODY_BYTES = 16 * 1024 * 1024
POST_BODY_READ_TIMEOUT = 10.0


class CodeyHTTPServer(ThreadingHTTPServer):
    """Keep routine browser disconnects out of the local server log."""

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


STATE = AppContext(DEFAULT_STATE_HOME)
STATE.providers.ghost_learning_provider_factory = connect_fresh_provider_tab
STATE.providers.ghost_router_provider_factory = connect_fresh_provider_tab
provider_controls.set_teach_handler(STATE.handle_control_teach)
provider_controls.set_doctor_handler(STATE.handle_profile_doctor)
provider_flow.set_recovery_handler(STATE.handle_flow_recovery)


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

def _run_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    run_id: str = "",
) -> None:
    deps = TaskRunDeps(
        state=STATE,
        agent_run=agent_run,
        collect_changes=collect_changes,
        run_review=lambda **kwargs: app_services.run_review(STATE, **kwargs),
        capture_provider_failure=capture_provider_failure,
        run_consensus=lambda **kwargs: app_services.run_consensus(STATE, **kwargs),
        run_project_audit=lambda **kwargs: app_services.run_project_audit(STATE, **kwargs),
        run_research_advisors=lambda **kwargs: app_services.run_research_advisors(STATE, **kwargs),
        project_facts=STATE.project_facts,
        work_checkpoints=STATE.work_checkpoints,
        workspace_revisions=STATE.workspace_revisions,
        run_ledgers=STATE.run_ledgers,
        run_traces=STATE.run_traces,
        evidence_ledgers=STATE.evidence_ledgers,
        managed_outputs=STATE.managed_outputs,
        knowledge_store=STATE.knowledge_store,
        is_git_repository=is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
        ghost_learning_provider_factory=STATE.providers.ghost_learning_provider_factory,
        ghost_router_provider_factory=STATE.providers.ghost_router_provider_factory,
        runtime_mutations=STATE.runtime_mutations,
        runtime_effects=STATE.runtime_effects,
    )
    try:
        run_task_submission(
            deps,
            TaskSubmission(
                session_id=session_id,
                project=project,
                task=task,
                max_turns=max_turns,
                continue_task=continue_task,
                provider_id=provider_id,
                intent=intent,
                run_id=run_id,
            )
        )
    finally:
        if _should_wait_for_local_ghost_sleep(STATE.state_home):
            STATE.wait_for_ghost_sleep()
        STATE.kick_self_repair()


def _submit_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    *,
    abort_if_stopped: bool = False,
) -> str | None:
    reserved = STATE.reserve_run(
        session_id=session_id,
        project=project,
        task=task,
        provider_id=provider_id,
        abort_if_stopped=abort_if_stopped,
    )
    if reserved is None:
        return None
    try:
        accepted = submit_browser_task(
            _run_task,
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
            reserved.run_id,
        )
    except Exception:
        STATE.release_run(reserved.run_id)
        raise
    if not accepted:
        STATE.release_run(reserved.run_id)
        raise BrowserWorkerBusy("browser worker busy: queue full")
    STATE.expire_stale_shell_approvals(reserved.run_id)
    return reserved.run_id


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
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        # Fast path; the authoritative guard is the atomic
        # abort_if_stopped reservation below, which closes the race where a
        # Stop lands between this peek and the reserve.
        if STATE.run_registry.stop_flag.is_set():
            return None
        active = STATE.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        run_id = _submit_task(
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
            abort_if_stopped=True,
        )
        if run_id is not None:
            return run_id
        active = STATE.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        if time.monotonic() >= deadline:
            return None
        remaining = max(0.0, deadline - time.monotonic())
        STATE.run_registry.wait_for_slot(remaining)


def _pick_folder_response(_ctx: AppContext, body: dict) -> tuple[int, dict]:
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


def _run_submit_route(_ctx: AppContext, body: dict) -> tuple[int, dict]:
    return app_api.run_submit_response(body, _submit_task)


def _shell_approval_route(ctx: AppContext, body: dict) -> tuple[int, dict]:
    return app_api.shell_approval_response(
        ctx,
        body,
        submit_task_after_slot_release=_submit_task_after_slot_release,
        continuation_retry_after=int(SHELL_CONTINUATION_IDLE_TIMEOUT),
    )


_GET_ROUTES = {
    "/api/state": lambda ctx, _query: (200, ctx.run_state_payload()),
    "/api/ui_state": lambda ctx, _query: app_api.ui_state_response(ctx),
    "/api/providers": lambda ctx, _query: app_api.providers_response(ctx),
    "/api/provider_catalog": lambda _ctx, _query: app_api.provider_catalog_response(),
    "/api/local_provider": lambda _ctx, _query: app_api.local_provider_response(),
    "/api/research/graph": app_api.research_graph_response,
    "/api/research/note": app_api.research_note_response,
    "/api/run_details": app_api.run_details_response,
    "/api/ghost/summary": app_api.ghost_summary_response,
    "/api/ghost/export": lambda ctx, _query: app_api.ghost_export_response(ctx),
}


_POST_ROUTES = {
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
    "/api/changes/restore": app_api.restore_changes_response,
    "/api/shell_approval": _shell_approval_route,
    "/api/teach/resume": app_api.teach_resume_response,
    "/api/new_chat": app_api.new_chat_response,
    "/api/stop": lambda ctx, _body: app_api.stop_response(ctx),
}


# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"Codey/{__version__}"

    def log_message(self, fmt, *args):
        # Quiet the default access log.
        pass

    def _request_origin_allowed(self) -> bool:
        return request_origin_allowed(self)

    def _deny_foreign_origin(self) -> None:
        self._send_json(403, {"error": "cross-origin request refused"})

    def _send_json(self, status: int, payload: dict) -> None:
        send_json(self, status, payload)

    def _send_file(self, path: Path, ctype: str) -> None:
        send_file(self, path, ctype)

    def _send_index(self) -> None:
        send_index(self)

    def _read_post_body(self, length: int) -> bytes | None:
        """Bounded body read: slow clients get 408 instead of a thread."""
        if not length:
            return b""
        connection = getattr(self, "connection", None)
        set_timeout = getattr(connection, "settimeout", None)
        previous_timeout: object = None
        if callable(set_timeout):
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
                return self.rfile.read(length)
            except (TimeoutError, socket.timeout):
                self._send_json(408, {"error": "request body timeout"})
                return None
            except Exception:
                self._send_json(400, {"error": "request body unreadable"})
                return None
        finally:
            if callable(set_timeout):
                try:
                    connection.settimeout(previous_timeout)
                except Exception:
                    pass

    def do_GET(self) -> None:  # noqa: N802
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
                self._send_file(asset[0], asset[1])
            return
        if url.path == "/icon.ico":
            icon = WEB_DIR / "icon.ico"
            if icon.is_file():
                self._send_file(icon, "image/x-icon")
            else:
                self.send_response(404)
                self.end_headers()
            return
        route = _GET_ROUTES.get(url.path)
        if route is not None:
            try:
                status, payload = route(STATE, parse_qs(url.query))
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length < 0:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length > MAX_POST_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
        raw = self._read_post_body(length)
        if raw is None:
            return
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return

        route = _POST_ROUTES.get(url.path)
        if route is not None:
            try:
                status, payload = route(STATE, body)
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
        replay_cursor = sse_replay_cursor(self.headers.get("Last-Event-ID"))
        q = STATE.subscribe()
        try:
            if not self._write_sse_event({"type": "hello", "status": STATE.run_status()}):
                return
            if replay_cursor is not None:
                for event_id, replay in STATE.replay_events_after(
                    replay_cursor,
                    max_event_id=q.replay_cutoff,
                ):
                    if not self._write_sse_event(replay, event_id=event_id):
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
                if not self._write_sse_event(ev, event_id=getattr(ev, "event_id", 0)):
                    break
        finally:
            STATE.unsubscribe(q)

    def _write_sse_event(self, event: dict, *, event_id: int = 0) -> bool:
        return write_sse_event(self, event, event_id=event_id)


def _wait_for_manual_browser(url: str, exc: Exception) -> None:
    print(f"[codey] Could not open native window: {exc}")
    print(f"[codey] Open this URL in your browser instead: {url}")
    if sys.platform == "win32":
        print("[codey] If needed, install Microsoft Edge WebView2 Runtime.")
    print("[codey] Press Ctrl+C to stop.")
    while True:
        time.sleep(3600)


def serve(host: str = "127.0.0.1", port: int = 5173) -> None:
    httpd = CodeyHTTPServer((host, port), Handler)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"[codey] UI ready: {url}")

    def _run_httpd() -> None:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    threading.Thread(target=_run_httpd, daemon=True).start()
    app_services.start_provider_warmup(STATE)

    def _run_webview() -> None:
        import webview

        icon = WEB_DIR / "icon.ico"
        webview.create_window("Codey", url, width=1380, height=900)
        start_kwargs = {
            "private_mode": False,
            "storage_path": str(DEFAULT_STATE_HOME / "webview"),
        }
        if icon.is_file():
            start_kwargs["icon"] = str(icon)
        webview.start(**start_kwargs)

    try:
        _run_webview()
    except KeyboardInterrupt:
        print("\n[codey] shutting down")
    except Exception as exc:
        try:
            _wait_for_manual_browser(url, exc)
        except KeyboardInterrupt:
            print("\n[codey] shutting down")
    finally:
        httpd.shutdown()
