"""Tiny HTTP + SSE server that drives the agent from a browser UI.

No external deps — just the standard library plus Playwright (already used).

Endpoints
    GET  /                serves codey/web/index.html
    GET  /api/state       returns current run state as JSON
    POST /api/run         body {project, task, max_turns} → starts agent in
                          a background thread, returns {ok:true}
    GET  /api/changes     query {project} → returns git status + diff
    POST /api/changes     body {project} → returns git status + diff
    POST /api/stop        request cooperative stop of the current task
    GET  /api/events      Server-Sent Events stream of log lines

A single Codey instance can run one task at a time; while a task is running
new /api/run calls return 409.
"""

from __future__ import annotations

import json
import queue
import difflib
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey.agent import DEFAULT_MAX_TURNS, RunResult, run as agent_run
from codey.browser import open_deepseek

WEB_DIR = Path(__file__).parent / "web"
FOLDER_DIALOG_LOCK = threading.Lock()
GIT_TIMEOUT = 10
MAX_DIFF_CHARS = 240_000
MAX_UNTRACKED_DIFF_BYTES = 120_000


def _run_git(project: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT,
        check=False,
    )


def parse_git_status(short_status: str) -> list[dict]:
    files: list[dict] = []
    for line in short_status.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "M"
        path = line[3:].strip() if len(line) > 3 else line[2:].strip()
        files.append({"path": path, "status": status, "additions": 0, "deletions": 0})
    return files


def _merge_numstat(stats: dict[str, dict[str, int]], text: str) -> None:
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        item = stats.setdefault(path, {"additions": 0, "deletions": 0})
        if added.isdigit():
            item["additions"] += int(added)
        if deleted.isdigit():
            item["deletions"] += int(deleted)


def _untracked_file_diff(root: Path, rel: str) -> tuple[str, int] | None:
    path = (root / rel).resolve()
    try:
        if not path.is_file() or path.stat().st_size > MAX_UNTRACKED_DIFF_BYTES:
            return None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.splitlines(keepends=True)
    rel_posix = rel.replace("\\", "/")
    diff = difflib.unified_diff(
        [],
        lines,
        fromfile="/dev/null",
        tofile=f"b/{rel_posix}",
        lineterm="",
    )
    return "\n".join(diff), len(text.splitlines())


def collect_git_changes(project: str | Path | None) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    root = Path(project).expanduser().resolve()
    if not root.exists():
        return {"ok": False, "error": "project not found", "files": [], "diff": ""}

    try:
        top = _run_git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"git unavailable: {exc}", "files": [], "diff": ""}
    if top.returncode != 0:
        return {"ok": False, "error": "not a git repository", "files": [], "diff": ""}
    git_root = Path(top.stdout.strip()).resolve()

    try:
        status_proc = _run_git(git_root, ["status", "--short"])
        unstaged_num = _run_git(git_root, ["diff", "--numstat"])
        staged_num = _run_git(git_root, ["diff", "--cached", "--numstat"])
        unstaged_diff = _run_git(git_root, ["diff", "--no-ext-diff", "--"])
        staged_diff = _run_git(git_root, ["diff", "--cached", "--no-ext-diff", "--"])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git command timed out", "files": [], "diff": ""}

    files = parse_git_status(status_proc.stdout)
    stats: dict[str, dict[str, int]] = {}
    _merge_numstat(stats, unstaged_num.stdout)
    _merge_numstat(stats, staged_num.stdout)

    diff_parts: list[str] = []
    if staged_diff.stdout:
        diff_parts.append(staged_diff.stdout.rstrip())
    if unstaged_diff.stdout:
        diff_parts.append(unstaged_diff.stdout.rstrip())

    for file in files:
        path = file["path"]
        stat = stats.get(path)
        if stat:
            file.update(stat)
        if file["status"] == "??":
            untracked = _untracked_file_diff(git_root, path)
            if untracked:
                diff_text, additions = untracked
                file["additions"] = additions
                file["deletions"] = 0
                diff_parts.append(diff_text)

    diff = "\n\n".join(part for part in diff_parts if part)
    truncated = len(diff) > MAX_DIFF_CHARS
    if truncated:
        diff = diff[:MAX_DIFF_CHARS].rstrip() + "\n\n... diff truncated by Codey"
    return {
        "ok": True,
        "root": str(git_root),
        "files": files,
        "changed_count": len(files),
        "diff": diff,
        "truncated": truncated,
    }


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
        self.last_stop_reason: str | None = None
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
) -> None:
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
        "max_turns": max_turns,
        "continue_task": continue_task,
    })

    def on_event(msg: str) -> None:
        STATE.emit({"type": "log", "session_id": session_id, "level": "info", "text": msg})

    try:
        sess = STATE.get_session()
        if project:
            result = agent_run(
                sess.page,
                Path(project),
                task,
                max_turns=max_turns,
                on_event=on_event,
                stop_flag=STATE.stop_flag,
                fresh_chat=False,
            )
        else:
            # Plain chat: just send + capture the reply, no tools executed.
            from codey.deepseek import chat as ds_chat
            reply = ds_chat(sess.page, task)
            STATE.emit({"type": "reply", "session_id": session_id, "text": reply})
            result = RunResult("", "done", 1)
        STATE.last_summary = result.summary
        STATE.last_stop_reason = result.stop_reason
        STATE.status = "done"
        STATE.emit({
            "type": "task_done",
            "session_id": session_id,
            "summary": result.summary,
            "stop_reason": result.stop_reason,
            "turns": result.turns,
            "max_turns": max_turns,
        })
    except Exception as exc:
        STATE.status = "error"
        STATE.last_stop_reason = "error"
        STATE.emit({"type": "log", "session_id": session_id, "level": "error", "text": f"ERROR: {exc!r}"})
        STATE.emit({
            "type": "task_done",
            "session_id": session_id,
            "summary": f"ERROR: {exc}",
            "stop_reason": "error",
            "turns": 0,
            "max_turns": max_turns,
        })
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
                "stop_reason": STATE.last_stop_reason,
            })
            return
        if url.path == "/api/changes":
            query = parse_qs(url.query)
            project = (query.get("project") or [""])[0].strip()
            payload = collect_git_changes(project)
            self._send_json(200 if payload.get("ok") else 400, payload)
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
            continue_task = bool(body.get("continue_task"))
            try:
                max_turns = int(body.get("max_turns") or DEFAULT_MAX_TURNS)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid max_turns"}); return
            max_turns = max(1, min(max_turns, 500))
            if not task:
                self._send_json(400, {"error": "task required"}); return
            if project:
                Path(project).mkdir(parents=True, exist_ok=True)
            threading.Thread(
                target=_run_task,
                args=(session_id, project, task, max_turns, continue_task),
                daemon=True,
            ).start()
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/pick_folder":
            mode = str(body.get("mode") or "open").strip().lower()
            if mode not in {"open", "new"}:
                self._send_json(400, {"error": "invalid mode"}); return
            initial = str(body.get("initial") or "").strip() or None
            try:
                path = pick_folder(mode=mode, initial=initial)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)}); return
            if not path:
                self._send_json(200, {"ok": False, "cancelled": True}); return
            self._send_json(200, {"ok": True, "path": path, "name": Path(path).name or path})
            return
        if url.path == "/api/changes":
            project = (body.get("project") or "").strip()
            payload = collect_git_changes(project)
            self._send_json(200 if payload.get("ok") else 400, payload)
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
