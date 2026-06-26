"""Tiny HTTP + SSE server that drives the agent from a browser UI.

No external deps — just the standard library plus Playwright (already used).

Endpoints
    GET  /                serves codey/web/index.html
    GET  /api/state       returns current run state as JSON
    POST /api/run         body {project, task, max_turns} → starts agent in
                          a background thread, returns {ok:true}
    POST /api/stop        request cooperative stop of the current task
    GET  /api/events      Server-Sent Events stream of log lines

A single Codey instance can run one task at a time; while a task is running
new /api/run calls return 409.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from codey.agent import run as agent_run
from codey.browser import open_deepseek

WEB_DIR = Path(__file__).parent / "web"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: queue.Queue[dict] = queue.Queue()
        self.subscribers: list[queue.Queue[dict]] = []
        self.busy = False
        self.project: str | None = None
        self.task: str | None = None
        self.status: str = "idle"
        self.last_summary: str | None = None
        self.stop_flag = threading.Event()
        self._session = None

    def emit(self, event: dict) -> None:
        with self.lock:
            for sub in list(self.subscribers):
                try:
                    sub.put_nowait(event)
                except Exception:
                    pass

    def subscribe(self) -> queue.Queue[dict]:
        q: queue.Queue[dict] = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def get_session(self):
        if self._session is None:
            self.status = "connecting"
            self.emit({"type": "status", "status": "connecting"})
            self._session = open_deepseek()
            self.emit({"type": "log", "level": "info", "text": f"Edge connected: {self._session.page.url}"})
        return self._session


STATE = State()


# ----------------------------------------------------------- task runner ---

DEFAULT_MAX_TURNS = 30


def _run_task(session_id: str, project: str | None, task: str) -> None:
    STATE.busy = True
    STATE.project = project
    STATE.task = task
    STATE.status = "running"
    STATE.stop_flag.clear()
    STATE.emit({
        "type": "task_start",
        "session_id": session_id,
        "project": project,
        "task": task,
        "mode": "agent" if project else "chat",
    })

    def on_event(msg: str) -> None:
        STATE.emit({"type": "log", "session_id": session_id, "level": "info", "text": msg})

    try:
        sess = STATE.get_session()
        if project:
            summary = agent_run(
                sess.page,
                Path(project),
                task,
                max_turns=DEFAULT_MAX_TURNS,
                on_event=on_event,
                stop_flag=STATE.stop_flag,
                fresh_chat=False,
            )
        else:
            # Plain chat: just send + capture the reply, no tools executed.
            from codey.deepseek import chat as ds_chat
            reply = ds_chat(sess.page, task)
            STATE.emit({"type": "reply", "session_id": session_id, "text": reply})
            summary = ""
        STATE.last_summary = summary
        STATE.status = "done"
        STATE.emit({"type": "task_done", "session_id": session_id, "summary": summary})
    except Exception as exc:
        STATE.status = "error"
        STATE.emit({"type": "log", "session_id": session_id, "level": "error", "text": f"ERROR: {exc!r}"})
        STATE.emit({"type": "task_done", "session_id": session_id, "summary": f"ERROR: {exc}"})
    finally:
        STATE.busy = False


# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = "Codey/0.1"

    def log_message(self, fmt, *args):
        # Quiet the default access log.
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if url.path == "/api/state":
            self._send_json(200, {
                "busy": STATE.busy,
                "status": STATE.status,
                "project": STATE.project,
                "task": STATE.task,
                "summary": STATE.last_summary,
            })
            return
        if url.path == "/api/events":
            self._sse()
            return
        self.send_response(404); self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"error": "invalid json"}); return

        if url.path == "/api/run":
            if STATE.busy:
                self._send_json(409, {"error": "busy"}); return
            session_id = str(body.get("session_id") or "").strip() or "default"
            project = (body.get("project") or "").strip() or None
            task = (body.get("task") or "").strip()
            if not task:
                self._send_json(400, {"error": "task required"}); return
            if project:
                Path(project).mkdir(parents=True, exist_ok=True)
            threading.Thread(
                target=_run_task, args=(session_id, project, task), daemon=True
            ).start()
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/new_chat":
            try:
                from codey.deepseek import new_chat
                sess = STATE.get_session()
                new_chat(sess.page)
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if url.path == "/api/stop":
            STATE.stop_flag.set()
            STATE.emit({"type": "log", "level": "warn", "text": "stop requested"})
            self._send_json(200, {"ok": True})
            return
        self.send_response(404); self.end_headers()

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = STATE.subscribe()
        try:
            q.put_nowait({"type": "hello", "status": STATE.status})
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
                data = json.dumps(ev, ensure_ascii=False)
                try:
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            STATE.unsubscribe(q)


def serve(host: str = "127.0.0.1", port: int = 5173, open_in_browser: bool = True) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"[codey] UI ready: {url}")
    if open_in_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[codey] shutting down")
        httpd.shutdown()
