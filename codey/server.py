"""Tiny HTTP + SSE server that drives the agent from a native UI.

Requires pywebview in addition to the standard library plus Playwright
(already used).

Endpoints
    GET  /                serves codey/web/index.html
    GET  /api/state       returns current run state as JSON
    POST /api/run         body {project, task, provider, max_turns} → starts agent in
                          a background thread, returns {ok:true}
    GET  /api/changes     query {project} → returns git status + diff
    POST /api/changes     body {project} → returns git status + diff
    POST /api/shell_approval body {id, approved} → approve/reject shell request
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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey import provider_controls
from codey import __version__
from codey.agent import DEFAULT_MAX_TURNS, run as agent_run
from codey.browser_worker import submit as submit_browser_task
from codey.changes import ChangeTracker
from codey.handoff import ConversationContext
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    connect_existing_provider,
    connect_provider,
    provider_tab_availability,
)
from codey.provider_diagnostics import ProviderFailure, capture_provider_failure
from codey.review import (
    ReviewResult,
    parse_review_with_repair,
    render_review_prompt,
)
from codey.task_runner import TaskRequest, TaskRunner

WEB_DIR = Path(__file__).parent / "web"
FOLDER_DIALOG_LOCK = threading.Lock()
GIT_TIMEOUT = 10
MAX_DIFF_CHARS = 240_000
MAX_UNTRACKED_DIFF_BYTES = 120_000
SHELL_TIMEOUT = 120
SHELL_OUTPUT_LIMIT = 24_000
REVIEW_TIMEOUT = 300.0
REVIEW_FIX_TURNS = 12
REVIEW_LOG_LINES = 80
CONTROL_TEACH_TIMEOUT = 300.0
MAX_CONVERSATION_STATES = 32
CHANGE_EXCLUDED_PATH_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "dist",
    "build",
}


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


def is_displayable_change_path(path: str) -> bool:
    normalized = (path or "").replace("\\", "/").strip("/")
    parts = [part for part in normalized.split("/") if part and part != "->"]
    return not any(part in CHANGE_EXCLUDED_PATH_PARTS for part in parts)


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

    files = [file for file in parse_git_status(status_proc.stdout) if is_displayable_change_path(file["path"])]
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
        "mode": "git",
        "vcs": {"git_available": True, "is_repo": True},
        "root": str(git_root),
        "files": files,
        "changed_count": len(files),
        "diff": diff,
        "truncated": truncated,
    }


def _empty_snapshot_changes(project: str | Path | None, error: str | None = None) -> dict:
    root = str(Path(project).expanduser().resolve()) if project else ""
    return {
        "ok": True,
        "mode": "snapshot",
        "vcs": {"git_available": error != "git unavailable", "is_repo": False},
        "root": root,
        "files": [],
        "changed_count": 0,
        "diff": "",
        "truncated": False,
    }


def collect_changes(project: str | Path | None, tracker: ChangeTracker | None = None) -> dict:
    if not project:
        return {"ok": False, "error": "project required", "files": [], "diff": ""}
    git_data = collect_git_changes(project)
    if git_data.get("ok"):
        return git_data
    if tracker is not None:
        data = tracker.collect()
        data["vcs"] = {
            "git_available": not str(git_data.get("error", "")).startswith("git unavailable"),
            "is_repo": False,
        }
        return data
    if git_data.get("error") in {"not a git repository"} or str(git_data.get("error", "")).startswith("git unavailable"):
        return _empty_snapshot_changes(project, "git unavailable" if str(git_data.get("error", "")).startswith("git unavailable") else None)
    return git_data


def reviewer_candidates(writer_id: str) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    return tuple(provider_id for provider_id in PROVIDER_LABELS if provider_id != writer)


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_availability() -> dict[str, bool]:
    return provider_tab_availability()


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict]:
    statuses = statuses or {}
    return [
        {"id": provider_id, "label": label, "available": bool(statuses.get(provider_id))}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_status_update(provider_id: str, available: bool) -> list[dict]:
    return [{
        "id": provider_id,
        "label": PROVIDER_LABELS.get(provider_id, provider_id),
        "available": available,
    }]


def _emit_review(session_id: str, text: str) -> None:
    STATE.emit({"type": "review", "session_id": session_id, "text": text})


def _run_review(
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    writer_id: str,
) -> tuple[str, ReviewResult] | None:
    last_error: Exception | None = None
    for reviewer_id in reviewer_candidates(writer_id):
        reviewer = None
        try:
            reviewer = connect_existing_provider(reviewer_id)
            STATE.set_provider_session(reviewer_id, None)
            reviewer.new_chat()
            prompt = render_review_prompt(
                project=project,
                task=task,
                writer_summary=writer_summary,
                changes=changes,
                recent_log=recent_log,
            )
            reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)
            review = parse_review_with_repair(
                reply,
                lambda repair: reviewer.send(repair, timeout=REVIEW_TIMEOUT),
            )
            label = review_label(reviewer_id)
            if review.approved:
                _emit_review(session_id, f"{label} approved")
            else:
                _emit_review(session_id, f"{label} suggested changes")
            return reviewer_id, review
        except Exception as exc:
            last_error = exc
        finally:
            if reviewer is not None:
                try:
                    reviewer.close()
                except Exception:
                    pass
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


def restore_snapshot_changes(
    project: str | Path | None,
    tracker: ChangeTracker | None,
    paths: list[str] | None = None,
) -> tuple[int, dict]:
    if not project:
        return 400, {"ok": False, "error": "project required"}
    if tracker is None:
        return 404, {"ok": False, "error": "no snapshot changes to restore"}
    result = tracker.restore(paths)
    payload = {
        "ok": result.ok,
        "restored": result.restored,
        "conflicts": result.conflicts,
        "error": result.error,
    }
    return (200 if result.ok else 409), payload


def _safe_project_cwd(project: str | Path, rel: str) -> Path:
    root = Path(project).expanduser().resolve()
    cwd = (root / (rel or ".")).resolve()
    if root not in cwd.parents and cwd != root:
        raise ValueError("cwd escapes project root")
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    return cwd


def execute_approved_shell(project: str | Path, rel: str, command: str) -> dict:
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "command required", "exit_code": None, "output": ""}
    try:
        cwd = _safe_project_cwd(project, rel)
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SHELL_TIMEOUT,
            shell=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"command timed out after {SHELL_TIMEOUT}s",
            "exit_code": None,
            "output": "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "exit_code": None, "output": ""}

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    truncated = len(output) > SHELL_OUTPUT_LIMIT
    if truncated:
        output = output[:SHELL_OUTPUT_LIMIT].rstrip() + "\n\n... output truncated by Codey"
    return {
        "ok": True,
        "error": None,
        "exit_code": proc.returncode,
        "output": output,
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
        self.provider_id = DEFAULT_PROVIDER_ID
        self.status: str = "idle"
        self.last_summary: str | None = None
        self.last_stop_reason: str | None = None
        self.last_provider_failure: ProviderFailure | None = None
        self.stop_flag = threading.Event()
        self.pending_shell: dict[str, dict] = {}
        self.pending_teach: dict[str, dict] = {}
        self.change_trackers: dict[str, ChangeTracker] = {}
        self.conversations: dict[str, ConversationContext] = {}
        self.provider_sessions: dict[str, str] = {}

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

    def get_provider(self, provider_id: str = DEFAULT_PROVIDER_ID):
        self.status = "connecting"
        self.emit({"type": "status", "status": "connecting"})
        provider = connect_provider(provider_id)
        self.emit({
            "type": "providers",
            "providers": provider_status_update(provider_id, True),
        })
        return provider

    def conversation_for(self, session_id: str) -> ConversationContext:
        with self.lock:
            context = self.conversations.pop(session_id, None)
            if context is None:
                if len(self.conversations) >= MAX_CONVERSATION_STATES:
                    oldest = next(iter(self.conversations))
                    self.conversations.pop(oldest)
                context = ConversationContext()
            self.conversations[session_id] = context
            return context

    def forget_conversation(self, session_id: str) -> None:
        with self.lock:
            self.conversations.pop(session_id, None)
            for provider_id, owner in list(self.provider_sessions.items()):
                if owner == session_id:
                    self.provider_sessions.pop(provider_id)

    def provider_session_changed(self, provider_id: str, session_id: str) -> bool:
        with self.lock:
            return self.provider_sessions.get(provider_id) != session_id

    def set_provider_session(self, provider_id: str, session_id: str | None) -> None:
        with self.lock:
            if session_id:
                self.provider_sessions[provider_id] = session_id
            else:
                self.provider_sessions.pop(provider_id, None)

    def handle_control_teach(self, request: provider_controls.ControlTeachRequest):
        while True:
            teach_id = "teach_" + uuid.uuid4().hex[:12]
            token = provider_controls.start_click_capture(request.page)
            pending = {
                "id": teach_id,
                "request": request,
                "token": token,
                "event": threading.Event(),
                "cancelled": False,
            }
            with self.lock:
                self.pending_teach[teach_id] = pending
            self.emit({
                "type": "teach_request",
                "session_id": request.session_id,
                "id": teach_id,
                "text": request.message,
            })
            if not pending["event"].wait(CONTROL_TEACH_TIMEOUT):
                with self.lock:
                    self.pending_teach.pop(teach_id, None)
                provider_controls.cancel_click_capture(request.page)
                raise TimeoutError("Timed out waiting for Resume")
            if pending.get("cancelled"):
                provider_controls.cancel_click_capture(request.page)
                raise provider_controls.ControlTeachCancelled("control teaching was cancelled")
            try:
                captured = provider_controls.finish_click_capture(
                    request.page,
                    token,
                    request.action,
                    timeout=1.0,
                )
                return provider_controls.resolve_captured_control(request, captured)
            except ValueError:
                continue
            finally:
                with self.lock:
                    self.pending_teach.pop(teach_id, None)


STATE = State()
provider_controls.set_teach_handler(STATE.handle_control_teach)


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
) -> None:
    runner = TaskRunner(
        STATE,
        agent_run=agent_run,
        collect_changes=collect_changes,
        run_review=_run_review,
        capture_provider_failure=capture_provider_failure,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
    )
    runner.run(TaskRequest(
        session_id=session_id,
        project=project,
        task=task,
        max_turns=max_turns,
        continue_task=continue_task,
        provider_id=provider_id,
    ))

# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"Codey/{__version__}"

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
        if url.path == "/icon.ico":
            icon = WEB_DIR / "icon.ico"
            if icon.is_file():
                self._send_file(icon, "image/x-icon")
            else:
                self.send_response(404)
                self.end_headers()
            return
        if url.path == "/api/state":
            self._send_json(200, {
                "busy": STATE.busy,
                "status": STATE.status,
                "project": STATE.project,
                "task": STATE.task,
                "summary": STATE.last_summary,
                "stop_reason": STATE.last_stop_reason,
                "provider": STATE.provider_id,
                "provider_failure": (
                    STATE.last_provider_failure.to_dict()
                    if STATE.last_provider_failure
                    else None
                ),
            })
            return
        if url.path == "/api/providers":
            try:
                statuses = provider_availability()
            except Exception:
                statuses = {}
            self._send_json(200, {
                "default": DEFAULT_PROVIDER_ID,
                "providers": provider_payload(statuses),
            })
            return
        if url.path == "/api/changes":
            query = parse_qs(url.query)
            project = (query.get("project") or [""])[0].strip()
            key = str(Path(project).expanduser().resolve()) if project else ""
            with STATE.lock:
                tracker = STATE.change_trackers.get(key)
            payload = collect_changes(project, tracker)
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
            provider_id = str(body.get("provider") or DEFAULT_PROVIDER_ID).strip().lower()
            try:
                max_turns = int(body.get("max_turns") or DEFAULT_MAX_TURNS)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid max_turns"}); return
            max_turns = max(1, min(max_turns, 500))
            if not task:
                self._send_json(400, {"error": "task required"}); return
            if provider_id not in PROVIDER_LABELS:
                self._send_json(400, {"error": f"unsupported provider: {provider_id}"}); return
            if project:
                Path(project).mkdir(parents=True, exist_ok=True)
            submit_browser_task(
                _run_task,
                session_id,
                project,
                task,
                max_turns,
                continue_task,
                provider_id,
            )
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
            key = str(Path(project).expanduser().resolve()) if project else ""
            with STATE.lock:
                tracker = STATE.change_trackers.get(key)
            payload = collect_changes(project, tracker)
            self._send_json(200 if payload.get("ok") else 400, payload)
            return
        if url.path == "/api/changes/restore":
            project = (body.get("project") or "").strip()
            if not project:
                self._send_json(400, {"ok": False, "error": "project required"})
                return
            paths = body.get("paths")
            if paths is not None and not isinstance(paths, list):
                self._send_json(400, {"ok": False, "error": "paths must be a list"})
                return
            clean_paths = [str(path) for path in paths] if paths is not None else None
            key = str(Path(project).expanduser().resolve())
            with STATE.lock:
                tracker = STATE.change_trackers.get(key)
            status, payload = restore_snapshot_changes(project, tracker, clean_paths)
            self._send_json(status, payload)
            return
        if url.path == "/api/shell_approval":
            approval_id = str(body.get("id") or "").strip()
            approved = body.get("approved") is True
            with STATE.lock:
                pending = STATE.pending_shell.pop(approval_id, None)
            if not pending:
                self._send_json(404, {"error": "approval not found"}); return
            session_id = pending["session_id"]
            command = pending["command"]
            if not approved:
                STATE.emit({
                    "type": "shell_result",
                    "session_id": session_id,
                    "id": approval_id,
                    "approved": False,
                    "command": command,
                    "cwd": pending["cwd"],
                    "output": "用户已拒绝执行该命令。",
                    "exit_code": None,
                })
                self._send_json(200, {"ok": True, "approved": False})
                return

            result = execute_approved_shell(pending["project"], pending["cwd"], command)
            STATE.emit({
                "type": "shell_result",
                "session_id": session_id,
                "id": approval_id,
                "approved": True,
                "command": command,
                "cwd": pending["cwd"],
                "output": result.get("output") or result.get("error") or "",
                "exit_code": result.get("exit_code"),
                "ok": result.get("ok"),
            })
            continued = False
            if pending.get("continue_after") and not STATE.busy:
                continuation = (
                    "Continue the interrupted task in this same conversation.\n"
                    "The user approved and ran this shell command:\n"
                    f"{command}\n\n"
                    f"Exit code: {result.get('exit_code')}\n"
                    "Output:\n"
                    f"{result.get('output') or result.get('error') or '(no output)'}\n\n"
                    "Use this result to continue the original task. If the task is complete,"
                    " reply with a JSON done tool call."
                )
                submit_browser_task(
                    _run_task,
                    session_id,
                    pending["project"],
                    continuation,
                    int(pending["max_turns"]),
                    True,
                    pending.get("provider") or DEFAULT_PROVIDER_ID,
                )
                continued = True
            self._send_json(200, {"ok": True, "approved": True, "continued": continued, "result": result})
            return
        if url.path == "/api/teach/resume":
            teach_id = str(body.get("id") or "").strip()
            with STATE.lock:
                pending = STATE.pending_teach.get(teach_id)
            if not pending:
                self._send_json(404, {"error": "pause not found"}); return
            pending["event"].set()
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/new_chat":
            session_id = str(body.get("session_id") or "").strip()
            if session_id:
                STATE.forget_conversation(session_id)
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/stop":
            STATE.stop_flag.set()
            with STATE.lock:
                pending_teach = list(STATE.pending_teach.values())
            for pending in pending_teach:
                pending["cancelled"] = True
                pending["event"].set()
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


def serve(host: str = "127.0.0.1", port: int = 5173) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"[codey] UI ready: {url}")

    def _run_httpd() -> None:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    threading.Thread(target=_run_httpd, daemon=True).start()

    def _run_webview() -> None:
        import webview

        icon = WEB_DIR / "icon.ico"
        webview.create_window("Codey", url, width=1380, height=900)
        if icon.is_file():
            webview.start(icon=str(icon))
        else:
            webview.start()

    try:
        _run_webview()
    except KeyboardInterrupt:
        print("\n[codey] shutting down")
    finally:
        httpd.shutdown()
