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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey import __version__
from codey.app import api as app_api
from codey.app import provider_services as provider_services
from codey.app import task_submit as task_submit
from codey.app.context import AppContext
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
from codey.storage.local_store import DEFAULT_STATE_HOME

FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_CONTINUATION_IDLE_TIMEOUT = task_submit.SHELL_CONTINUATION_IDLE_TIMEOUT
MAX_POST_BODY_BYTES = 16 * 1024 * 1024
POST_BODY_READ_TIMEOUT = 10.0


class CodeyHTTPServer(ThreadingHTTPServer):
    """Keep routine browser disconnects out of the local server log."""

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


STATE: AppContext | None = None
_STATE_LOCK = threading.Lock()


def _build_state() -> AppContext:
    from codey.app import sibling_probe
    from codey.app.provider_services import connect_fresh_provider_tab

    state = AppContext(DEFAULT_STATE_HOME)
    state.providers.ghost_learning_provider_factory = connect_fresh_provider_tab
    state.providers.ghost_router_provider_factory = connect_fresh_provider_tab
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
    task_submit.run_task(
        session_id,
        project,
        task,
        max_turns,
        continue_task,
        provider_id,
        intent,
        run_id,
        get_state=get_state,
    )


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
    )


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

    def _send_file(self, path: Path, ctype: str, *, immutable: bool = False) -> None:
        send_file(self, path, ctype, immutable=immutable)

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
            except TimeoutError:
                self._send_json(408, {"error": "request body timeout"})
                return None
            except Exception:
                self._send_json(400, {"error": "request body unreadable"})
                return None
        finally:
            if callable(set_timeout):
                with contextlib.suppress(Exception):
                    connection.settimeout(previous_timeout)

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
        replay_cursor = sse_replay_cursor(raw_header)
        state = get_state()
        q = state.subscribe()
        try:
            if not self._write_sse_event({"type": "hello", "status": state.run_status()}):
                return
            if replay_cursor is not None:
                for event_id, replay in state.replay_events_after(
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
            state.unsubscribe(q)

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
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()

    threading.Thread(target=_run_httpd, daemon=True).start()
    provider_services.start_provider_warmup(get_state(), delay_s=2.0)

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
